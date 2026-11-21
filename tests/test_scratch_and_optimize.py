import io
import json
import os
import plistlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stdout, suppress
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from arete import launchd, paths, poll, scratch, workspace
from arete.lab import experiment, optimize

PROMOTION_FLOOR = {"accept_rate": 0.8, "max_drop": 0.2, "min_saving": 0.1}


class BundledXPCScratchTests(unittest.TestCase):
    bundle = "/System/Library/Example.xpc"
    executable = bundle + "/Contents/MacOS/Example"
    host = scratch.Process(20, 120, 0, 20, "/Applications/Host")
    service = scratch.Process(30, 130, 0, 30, executable)

    def setUp(self):
        self.current = {self.host.identity: self.host, self.service.identity: self.service}
        self.responsible = {20: 20, 30: 20}
        self.outputs = {f"gui/{os.getuid()}": "services = {\n}\n",
                        f"user/{os.getuid()}": f"services = {{\n}}\nsubdomains = {{\n gui/{os.getuid()}\n}}\n"}
        self.service_domain(20, 30)
        lookup = Mock(side_effect=self.responsible.get)
        library = SimpleNamespace(responsibility_get_pid_responsible_for_pid=lookup)
        self.enterContext(patch.object(launchd, "ctypes", SimpleNamespace(CDLL=lambda _: library, c_int=int)))
        self.print_run = self.enterContext(patch.object(launchd.subprocess, "run", side_effect=self.launchctl_print))
        self.enterContext(patch.object(launchd, "sealed", side_effect={self.bundle, self.executable}.__contains__))
        self.enterContext(patch.object(launchd, "declared_program", return_value=self.executable))
        self.enterContext(patch.object(scratch, "processes", return_value=self.current))
        self.enterContext(patch.object(scratch, "identity_of", side_effect=lambda pid: pid + 100))
        self.data = {"attempt": {"workspace": "/fixture", "run_dir": "/fixture/run"},
                     "before": [self.host.identity], "producers": {}}

    def service_domain(self, host, service):
        self.outputs[f"pid/{host}"] = f"services = {{\n {service} 0 example\n}}\n"
        self.outputs[f"pid/{host}/example"] = f"\tpath = {self.bundle}\n\tpid = {service}\n"

    def launchctl_print(self, argv, **kwargs):
        output = self.outputs.get(argv[2])
        if isinstance(output, subprocess.TimeoutExpired):
            raise output
        if isinstance(output, subprocess.CompletedProcess):
            return output
        if output is None:
            pid = argv[2].removeprefix("pid/")
            return subprocess.CompletedProcess(argv, 112, "", f"Bad request.\nCould not find domain for pid: {pid}\n")
        return subprocess.CompletedProcess(argv, 0, output, "")

    def print_targets(self):
        targets = []
        for invocation in self.print_run.call_args_list:
            argv, = invocation.args
            self.assertEqual(argv[:2], [launchd.LAUNCHCTL, "print"])
            targets.append(argv[2])
        return targets

    def test_unrelated_host_is_exempt_after_listed_domains(self):
        decoy = scratch.Process(40, 140, -1, 40, "/Applications/Decoy")
        self.current[decoy.identity] = decoy
        self.responsible[decoy.pid] = decoy.pid
        self.assertEqual(scratch.holders(self.data), [])
        self.assertIn("pid/20/example", self.print_targets())
        self.assertNotIn("pid/40", self.print_targets())

        self.outputs[f"gui/{os.getuid()}"] += "subdomains = {\n pid/98\n}\n"
        self.service_domain(98, 30)
        self.print_run.reset_mock()
        table = launchd.Table(os.getuid(), self.responsible)
        self.assertEqual(table.job(30, bundled=True).domain, "pid/98")
        self.assertNotIn("pid/20", self.print_targets())

    def test_producer_and_descendant_hosts_keep_the_service_held(self):
        producer = scratch.Process(10, 110, 0, 10, "/producer")
        self.current[producer.identity] = producer
        self.data.update(before=[], producers={"verify": producer.group})
        self.responsible.update({10: 10, 20: 10, 30: 10})
        with patch.object(scratch.os, "killpg"):
            for host_pid, parent, group in ((10, 0, 10), (20, producer.identity, 20)):
                with self.subTest(host_pid=host_pid):
                    self.current[self.host.identity] = scratch.Process(20, 120, parent, group, self.host.executable)
                    self.outputs.pop("pid/20", None)
                    self.outputs.pop("pid/10", None)
                    self.service_domain(host_pid, 30)
                    self.print_run.reset_mock()
                    held = {h["pid"]: h["reason"] for h in scratch.holders(self.data)}
                    self.assertIn(f"pid/{host_pid}/example", self.print_targets())
                    self.assertEqual(held[30], scratch.UNCLASSIFIED_HOLDER)

    def test_print_budget_covers_all_targets_and_is_shared_across_lookups(self):
        other_host = scratch.Process(40, 140, -1, 40, "/Applications/Other")
        other_service = scratch.Process(50, 150, 0, 50, self.executable)
        self.current.update({140: other_host, 150: other_service})
        self.responsible.update({40: 40, 50: 40})
        self.service_domain(40, 50)
        self.outputs[f"gui/{os.getuid()}"] += "subdomains = {\n pid/98\n}\n"
        targets = [f"gui/{os.getuid()}", f"user/{os.getuid()}", "pid/98", "pid/20", "pid/30", "pid/20/example"]
        with patch.object(launchd, "MAX_PID_DOMAINS", len(targets) - 1):
            held = scratch.holders(self.data)
            self.assertEqual(held, [scratch.holder(service, scratch.UNCLASSIFIED_HOLDER)
                                    for service in (self.service, other_service)])
            self.assertEqual(self.print_targets(), targets[:-1])

        self.print_run.reset_mock()
        with patch.object(launchd, "MAX_PID_DOMAINS", len(targets)):
            table = launchd.Table(os.getuid(), self.responsible)
            job = table.job(30, bundled=True)
            self.assertEqual(job.domain, "pid/20")
            with self.assertRaises(launchd.Unreadable):
                table.job(50, bundled=True)
            self.assertEqual(table.job(30, bundled=True), job)
            self.assertEqual(self.print_targets(), targets)

    def test_host_domain_timeout_leaves_holder_unclassified(self):
        self.outputs["pid/20"] = subprocess.TimeoutExpired([launchd.LAUNCHCTL, "print", "pid/20"], 10)
        held = scratch.holders(self.data)
        self.assertEqual(held, [scratch.holder(self.service, scratch.UNCLASSIFIED_HOLDER)])
        targets = [f"gui/{os.getuid()}", f"user/{os.getuid()}", "pid/20", "pid/30"]
        self.assertEqual(self.print_targets(), targets)

        self.responsible[40] = 20
        self.current[140] = scratch.Process(40, 140, -1, 40, "/Applications/Other")
        self.service_domain(40, 30)
        self.print_run.reset_mock()
        self.assertEqual(scratch.holders(self.data), [])
        self.assertEqual(self.print_targets(), targets + ["pid/40", "pid/40/example"])

    def test_host_domain_print_failure_leaves_holder_unclassified(self):
        argv = [launchd.LAUNCHCTL, "print", "pid/20"]
        for status, stderr in ((-signal.SIGTERM, ""), (1, "Could not print domain: 1: Operation not permitted\n")):
            with self.subTest(status=status):
                self.print_run.reset_mock()
                self.outputs["pid/20"] = subprocess.CompletedProcess(argv, status, "", stderr)
                self.assertEqual(scratch.holders(self.data), [scratch.holder(self.service, scratch.UNCLASSIFIED_HOLDER)])
                self.assertEqual(self.print_targets(), [f"gui/{os.getuid()}", f"user/{os.getuid()}", "pid/20", "pid/30"])

    def test_service_exits_before_responsibility_lookup_leaves_holder_unclassified(self):
        self.responsible[self.service.pid] = -1  # Native return value for ESRCH.
        self.assertEqual(scratch.holders(self.data), [scratch.holder(self.service, scratch.UNCLASSIFIED_HOLDER)])
        self.assertEqual(self.print_targets(), [f"gui/{os.getuid()}", f"user/{os.getuid()}"])

    def test_missing_host_domains_leave_holder_orphaned(self):
        del self.outputs["pid/20"]
        self.assertEqual(scratch.holders(self.data), [scratch.holder(self.service, scratch.ORPHAN)])
        self.assertEqual(self.print_targets(), [f"gui/{os.getuid()}", f"user/{os.getuid()}", "pid/20", "pid/30"])

    def test_poll_finalization_releases_a_record_held_only_by_the_service(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            base = Path(folder).resolve()
            root, selector = base / (paths.SCRATCH_PREFIX + "fixture"), "a" * 64
            attempt = scratch.Attempt(selector, "fixture", "run", "verify", str(base), str(root), str(base), {}, {})
            data = {**self.data, "attempt": asdict(attempt), "version": 2, "state": "pending",
                    "source_mutations": False, "run": scratch.identity(base.stat())}

            @contextmanager
            def registry():
                with paths.directory_fd(base) as fd:
                    yield fd, b"fixture signing material"

            (base / (selector + ".lease")).touch()
            with (patch.object(scratch, "registry", registry),
                  patch.object(workspace, "made_record", return_value=base / "workspace-record"),
                  patch.object(scratch, "survey", return_value=scratch.View([], [], False)),
                  patch.object(scratch, "settle", return_value=[]), redirect_stdout(io.StringIO()) as output):
                with registry() as (fd, key):
                    scratch.store(fd, key, data)
                workspace.made_record(base).write_text(json.dumps({workspace.RETAINED_KEY: {"roots": [root.name]}}))
                with patch.dict(self.outputs, {"pid/20": "services = {\n}\n"}):
                    poll.retry_scratch()
                retained = scratch.records()[0]
                self.assertEqual(retained["state"], "retained")
                self.assertEqual(retained["diagnostic"]["holders"], [scratch.holder(self.service, scratch.ORPHAN)])
                self.assertIn(workspace.RETAINED_KEY, workspace.made_before(base))

                poll.retry_scratch()
                released = scratch.records()[0]
                self.assertEqual(released["state"], "done")
                self.assertNotIn(workspace.RETAINED_KEY, workspace.made_before(base))
                self.assertIn("preserved and finalized verification scratch", output.getvalue())


class ScratchRecordTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True))).resolve()
        self.enterContext(patch.object(scratch, "registry", self.registry))

    @contextmanager
    def registry(self):
        with paths.directory_fd(self.base) as fd:
            yield fd, b"fixture signing material"

    def record(self, attempt):
        with self.registry() as (fd, key):
            return scratch.load(fd, key, attempt.selector)

    def attempt(self, letter, **fields):
        workspace_dir, run_dir = self.base / f"workspace-{letter}", self.base / f"run-{letter}"
        workspace_dir.mkdir()
        run_dir.mkdir()
        attempt = scratch.Attempt(letter * 64, "fixture", run_dir.name, "verify", str(workspace_dir),
                                  str(workspace_dir / (paths.SCRATCH_PREFIX + "fixture")), str(run_dir), {}, {})
        data = {"attempt": asdict(attempt), "version": 2, "source_mutations": False, "state": "prepared",
                "run": scratch.identity(run_dir.stat()), "before": [], "producers": {}, **fields}
        with self.registry() as (fd, key):
            scratch.store(fd, key, data)
            os.close(os.open(attempt.selector + ".lease", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd))
        return attempt


class AttemptScratchHoldersTests(ScratchRecordTests):
    def setUp(self):
        super().setUp()
        self.processes = {}
        self.enterContext(patch.object(scratch, "processes", return_value=self.processes))
        self.enterContext(patch.object(scratch, "identity_of", side_effect=lambda pid: pid + 100))
        self.enterContext(patch.object(scratch.os, "killpg", side_effect=self.check_group))

    def check_group(self, group, signal):
        if not any(p.group == group for p in self.processes.values()):
            raise ProcessLookupError(group)

    def detached_child(self, attempt, producer, child):
        leader = scratch.Process(producer, producer + 100, 1, producer, "/bin/sh", uses_paths=False)
        self.processes[leader.identity] = leader
        scratch.producer_group(attempt, "verify", leader.group)
        detached = scratch.Process(child, child + 100, leader.identity, child, "/bin/sleep", orphaned=True,
                                   uses_paths=False)
        self.processes[detached.identity] = detached
        del self.processes[leader.identity]
        return detached

    def test_foreign_detached_child_does_not_hold_finished_attempt(self):
        first, second = self.attempt("a"), self.attempt("b")
        ended = self.detached_child(first, 10, 11)
        running = self.detached_child(second, 20, 21)
        del self.processes[ended.identity]

        self.assertEqual(scratch.end(first, Path(first.run_dir))[2], [])
        self.assertEqual(self.record(first)["state"], "done")
        self.assertEqual(scratch.holders(self.record(second)), [scratch.holder(running, scratch.ORPHAN)])

    def test_own_child_holds_after_leaving_its_producer_group(self):
        attempt = self.attempt("a")
        running = self.detached_child(attempt, 10, 11)

        self.assertEqual(scratch.end(attempt, Path(attempt.run_dir))[2], [attempt.root])
        self.assertEqual(self.record(attempt)["diagnostic"]["holders"], [scratch.holder(running, scratch.ORPHAN)])

    def test_sealed_instance_holds_while_it_uses_a_workspace_file(self):
        label = "com.apple.mdworker.shared"
        plist = self.base / "worker.plist"
        worker = scratch.Process(30, 130, 1, 30, "/usr/libexec/mdworker_shared", orphaned=True, uses_paths=False)
        plist.write_bytes(plistlib.dumps({"Label": label, "Program": worker.executable}))
        job = launchd.Job(f"gui/{os.getuid()}", label + ".new", str(plist), worker.pid)
        for letter, loaded in (("a", label), ("b", label + ".old")):
            with self.subTest(loaded=loaded):
                attempt = self.attempt(letter, state="retained", labels={"gui": [loaded]},
                                       producers={"verify": {"gated": True, "group": 10}},
                                       diagnostic=scratch.holders_diagnostic(
                                           scratch.WAITING, [scratch.holder(worker, scratch.SUBMITTED)]))
                ws = Path(attempt.workspace)
                made = ws / "workspace-record"
                made.write_text(json.dumps({workspace.RETAINED_KEY: {"roots": [Path(attempt.root).name]}}))
                self.processes[worker.identity] = worker
                with (patch.object(launchd.Table, "job", return_value=job),
                      patch.object(launchd, "sealed", return_value=True),
                      patch.object(workspace, "made_record", return_value=made),
                      patch.object(scratch, "survey", return_value=scratch.View([], [], False)),
                      patch.object(scratch, "settle", return_value=[]), redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(scratch.holders(self.record(attempt)), [])
                    self.processes[worker.identity] = replace(worker, uses_paths=True)
                    self.assertEqual(scratch.end(attempt, Path(attempt.run_dir))[2], [attempt.root])
                    self.assertEqual(self.record(attempt)["diagnostic"]["holders"],
                                     [scratch.holder(worker, scratch.USING_PATHS)])
                    self.processes[worker.identity] = worker
                    poll.retry_scratch()
                    self.assertEqual(self.record(attempt)["state"], "done")
                    self.assertNotIn(workspace.RETAINED_KEY, workspace.made_before(ws))
                    self.assertIn(f"preserved and finalized verification scratch {attempt.root}", output.getvalue())


@unittest.skipUnless(paths.ON_MAC, "requires macOS process inspection")
class NativeScratchHolderTests(ScratchRecordTests):
    daemon = """
import os
import sys
if (child := os.fork()) == 0:
    os.setsid()
    if sys.argv[1] == "double" and os.fork():
        os._exit(0)
    print(os.getpid(), flush=True)
    os.chdir("/")
    os.closerange(0, 256)
    os.execve("/bin/sleep", ["sleep", "45"], {})
if sys.argv[1] == "double":
    os.waitpid(child, 0)
"""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(launchd, "bundled", return_value=False))

    def register_cleanup(self, pid):
        owner = scratch.identity_of(pid)
        self.addCleanup(self.stop, pid, owner)
        return pid, owner

    def stop(self, pid, owner):
        if owner is not None and scratch.identity_of(pid) == owner:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
        until = time.monotonic() + 3
        while owner is not None and scratch.identity_of(pid) == owner and time.monotonic() < until:
            time.sleep(.01)
        self.assertTrue(owner is None or scratch.identity_of(pid) != owner, pid)

    def start(self, attempt, modern):
        command = [sys.executable, "-c", self.daemon, "double" if modern else "direct"]
        monitors = {}
        with scratch.producing(attempt):
            if modern:
                proc = scratch.spawn(command, actor_confined=True, cwd=attempt.workspace, stdout=subprocess.PIPE)
                monitors = scratch.monitors(self.record(attempt))
                for monitor in monitors.values():
                    self.register_cleanup(monitor)
            else:
                proc = subprocess.Popen(command, cwd=attempt.workspace, stdout=subprocess.PIPE, start_new_session=True)
        with proc:
            output, _ = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0)
        pid, owner = self.register_cleanup(int(output))
        processes = scratch.processes(attempt.workspace)
        self.assertIn(owner, processes)
        self.assertTrue(processes[owner].orphaned)
        self.assertIs(processes[owner].uses_paths, False)
        self.assertNotEqual(processes[owner].group, proc.pid)
        for identity, group in monitors.items():
            self.assertTrue(processes[identity].orphaned)
            self.assertEqual(processes[identity].group, group)
        return proc.pid, (pid, owner)

    def test_double_fork_keeps_its_attempt_after_the_producer_exits(self):
        first, second = self.attempt("a"), self.attempt("b")
        _, own = self.start(first, modern=True)
        _, foreign = self.start(second, modern=True)

        self.assertEqual(scratch.end(first, Path(first.run_dir))[2], [first.root])
        self.assertEqual(scratch.holding(first), [{"pid": own[0], "name": "sleep", "reason": scratch.MONITORED}])
        self.stop(*own)
        self.assertEqual(scratch.recover_pending(), [first.root])
        self.assertEqual(scratch.holding(second), [{"pid": foreign[0], "name": "sleep", "reason": scratch.MONITORED}])

    def test_legacy_record_holds_detached_child_during_recovery(self):
        attempt = self.attempt("a", before=list(scratch.process_ids()))
        group, child = self.start(attempt, modern=False)
        with self.registry() as (fd, key):
            data = scratch.load(fd, key, attempt.selector)
            data.update(state="retained", producers={"verify": {"gated": True, "group": group}})
            scratch.store(fd, key, data)

        with patch.object(launchd.Table, "job", return_value=None):
            self.assertEqual(scratch.recover_pending(), [])
            self.assertEqual(scratch.holding(attempt),
                             [{"pid": child[0], "name": "sleep", "reason": scratch.LEGACY_ORPHAN}])
            self.stop(*child)
            self.assertEqual(scratch.recover_pending(), [attempt.root])
            self.assertEqual(self.record(attempt)["state"], "done")


class OptimizeTests(unittest.TestCase):
    def decision(self, champion, candidate, reference=0.01, charged=0.0, seconds=1.0):
        def trials(arm, outcomes, cost):
            return [{"event": "trial", "task": "t", "split": "validation", "arm": arm, "repeat": n, "status": "done",
                     "accepted": ok, "charged_usd": charged, "reference_usd": cost, "repairs": 0, "calls": 2,
                     "tokens_in": 1, "tokens_out": 1, "wall_seconds": seconds}
                    for n, ok in enumerate(outcomes, 1)]

        accept = {"baseline": "champ", "split": "validation", "min_pairs": 6, "min_difference": 0.0,
                  "confidence": 0.95}
        rows = trials("champ", champion, 0.01) + trials("cand", candidate, reference)
        done = {experiment.key_of(t): t for t in rows}
        pair = experiment.pairing(done, "champ", "cand", "validation", 0.95)
        verdict = experiment.judge(pair, accept)
        return optimize.decide(experiment.arm_summary("cand", rows, 0.95), experiment.arm_summary("champ", rows, 0.95),
                               pair, verdict, PROMOTION_FLOOR)

    def test_a_clear_quality_win_is_promoted_on_quality(self):
        found = self.decision([False] * 6, [True] * 6)
        self.assertEqual((found["promote"], found["reason"]), (True, "promote on quality"))

    def test_a_cheaper_candidate_under_the_quality_floor_is_not_promoted(self):
        found = self.decision([True] * 6, [True, True, True, False, False, False], reference=0.002)
        self.assertEqual((found["promote"], found["reason"]), (False, "no promotion: under the quality floor"))

    def test_a_noisy_tie_at_equal_cost_is_not_a_win(self):
        found = self.decision([True] * 6, [True] * 6)
        self.assertEqual((found["promote"], found["reason"]), (False, "no promotion: noisy tie"))

    def test_missing_costs_and_tiny_samples_block_promotion(self):
        self.assertEqual(self.decision([False] * 6, [True] * 6, reference=None)["reason"],
                         "no promotion: missing costs")
        self.assertEqual(self.decision([False] * 3, [True] * 3)["reason"], "no promotion: insufficient evidence")

    def test_a_cheaper_candidate_at_held_quality_is_promoted_on_economics(self):
        found = self.decision([True] * 6, [True] * 6, reference=0.005)
        self.assertEqual((found["promote"], found["reason"]), (True, "promote on economics"))
        slower = self.decision([True] * 6, [True] * 6, reference=0.005, seconds=2.0)
        self.assertTrue(slower["promote"])
