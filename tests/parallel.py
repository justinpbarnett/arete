import argparse
import fcntl
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import import_module
from unittest.loader import _FailedTest
import contextlib

KEPT_LINES = 40
DEFAULT_CLASS_TIMEOUT = 10
STACK_GRACE = 1
LOAD_POLL_INTERVAL = 5
LOAD_WAIT_BUDGET = 5 * 60
HISTORY = os.path.join(tempfile.gettempdir(), "arete-parallel-durations.json")
TESTS = os.path.dirname(os.path.abspath(__file__))
STACK_SITE = os.path.join(TESTS, "_parallel_site")

if __spec__ is None:
    sys.path.insert(0, os.path.dirname(TESTS))
_test_stacks = import_module("arete._test_stacks")
lanes = import_module("arete.lanes")
THREAD = re.compile(r"(?:Current thread|Thread) (0x[0-9a-f]+) \(most recent call first\):")


def stack_env(env):
    import atexit
    from pathlib import Path
    from pwd import getpwuid

    from arete.local_paths import SCRATCH_ENV, inside_scratch

    env = {**env, "ARETE_DESKTOP_NOTIFY": "0"}
    # Preserve scratch selectors so worker startup can validate them.
    if not env.get(SCRATCH_ENV):
        home = Path((env["HOME"] if "HOME" in env else getpwuid(os.getuid()).pw_dir) or os.sep)
        config = Path(env.get("ARETE_CONFIG_DIR", home / ".config" / "arete"))
        state = Path(env.get("ARETE_STATE_DIR", home / ".local" / "share" / "arete"))
        if not (inside_scratch(config) or inside_scratch(state)):
            temporary = tempfile.TemporaryDirectory(prefix="arete-test-state-", ignore_cleanup_errors=True)
            atexit.register(temporary.cleanup)
            state = Path(temporary.name)
            config = state / "config"
            logs = state / "logs"
            config.mkdir()
            logs.mkdir()
            env.update(ARETE_STATE_DIR=str(state), ARETE_CONFIG_DIR=str(config), ARETE_LOG_DIR=str(logs))
    if not env.get(_test_stacks.PREFIX_ENV):
        return env
    pythonpath = env.get("PYTHONPATH")
    paths = pythonpath.split(os.pathsep) if pythonpath else []
    if STACK_SITE in paths:
        return env
    return {**env, "PYTHONPATH": os.pathsep.join([STACK_SITE, *paths])}


def main_thread_first(stack, ident):
    threads = stack.split("\n\n")
    for index, thread in enumerate(threads):
        heading = THREAD.fullmatch(thread.partition("\n")[0])
        if heading and int(heading[1], 16) == ident:
            threads.insert(0, threads.pop(index))
            break
    return "\n\n".join(threads)


def failed(report):
    return any(report.get(kind) for kind in ("timeout", "errors", "failures", "unexpected"))


def class_load_factor():
    try:
        load = os.getloadavg()[0]
    except OSError:
        return 1.0
    cpu_count = os.cpu_count()
    return min(4.0, max(1.0, load / cpu_count)) if cpu_count is not None else 1.0


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def end_with_runner(lifeline):
    try:
        if os.read(lifeline, 1):
            return
    except OSError:
        return
    if os.getpgrp() == os.getpid():
        os.killpg(os.getpgrp(), signal.SIGKILL)
    os.kill(os.getpid(), signal.SIGKILL)


def run_one(identifier, start, result_file, lifeline):
    os.set_inheritable(lifeline, False)
    threading.Thread(target=end_with_runner, args=(lifeline,), daemon=True).start()
    sys.path.insert(0, start)
    result = unittest.TestResult()
    unittest.defaultTestLoader.loadTestsFromName(identifier).run(result)
    report = {"ran": result.testsRun, "errors": [[str(t), text] for t, text in result.errors],
              "failures": [[str(t), text] for t, text in result.failures],
              "skipped": [[str(t), why] for t, why in result.skipped],
              "unexpected": [str(t) for t in result.unexpectedSuccesses]}
    with open(result_file + ".tmp", "w") as handle:
        json.dump(report, handle)
    os.replace(result_file + ".tmp", result_file)
    return 0


@dataclass
class Batch:
    identifier: str
    count: int
    serial: bool = False
    timeout: float = DEFAULT_CLASS_TIMEOUT


class Runner:
    def __init__(self, args):
        self.args = args
        self.lock = threading.RLock()
        self.live = {}
        self.stopping = False
        self.in_flight = []
        self.results = {}
        self.crowded = []
        self.first_failures = {}
        self.first_solo_timeouts = {}
        self.retry_loads = {}
        try:
            with open(HISTORY) as handle:
                history = json.load(handle)
        except (OSError, ValueError):
            history = {}
        self.history = history if isinstance(history, dict) else {}

    def kill(self, process):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)

    def stop(self, *args):
        with self.lock:
            self.stopping = True
            self.in_flight = list(self.live.values())
            for process in self.live:
                self.kill(process)

    def output(self, path):
        with open(path, "rb") as handle:
            lines = handle.read().decode(errors="replace").splitlines()
        if len(lines) <= KEPT_LINES:
            return "\n".join(lines)
        dropped = len(lines) - KEPT_LINES
        note = f"... {dropped} earlier output lines dropped; all of them are in {path} ..."
        return "\n".join([note, *lines[-KEPT_LINES:]])

    def request_stacks(self, prefix):
        deadline = time.monotonic() + STACK_GRACE
        requested = []
        try:
            entries = os.scandir(self.folder)
        except OSError:
            return requested
        with entries:
            for entry in entries:
                if self.stopping or time.monotonic() >= deadline:
                    break
                if not entry.name.startswith(prefix):
                    continue
                suffix = entry.name[len(prefix):]
                if not suffix.isascii() or not suffix.isdecimal():
                    continue
                pid = int(suffix)
                if pid <= 1:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    with open(entry.path, "rb") as handle:
                        try:
                            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        except BlockingIOError:
                            if self.stopping:
                                break
                            # An exec can reset the handler after this check. Unhandled SIGURG is ignored.
                            os.kill(pid, _test_stacks.DUMP_SIGNAL)
                            requested.append((pid, entry.path))
                except OSError:
                    continue
        if requested and not self.stopping:
            time.sleep(max(0, deadline - time.monotonic()))
        return requested

    def stack_output(self, requested, worker):
        dumps = []
        for pid, path in sorted(requested, key=lambda item: (item[0] != worker, item[0])):
            try:
                with open(path, "rb") as handle:
                    size = os.fstat(handle.fileno()).st_size
                    ident, _, stack = handle.read(size).decode(errors="replace").partition("\n")
                    stack = main_thread_first(stack.rstrip(), int(ident, 16))
            except (OSError, ValueError):
                continue
            if stack:
                role = "class" if pid == worker else "child"
                dumps.append(f"Python {role} stack (PID {pid})\n{stack}")
        return "\n\n".join(dumps)

    def child(self, item, stretch=1):
        index, batch = item
        result_file = os.path.join(self.folder, f"{index}.json")
        output_file = os.path.join(self.folder, f"{index}.out")
        stack_prefix = f"{index}.stack."
        env = stack_env({**os.environ, "ARETE_DRIVE_FLUSH": "0",
                         _test_stacks.PREFIX_ENV: os.path.join(self.folder, stack_prefix)})
        tail = ["--one", batch.identifier, "--start", self.args.start, "--result", result_file,
                "--lifeline", str(self.lifeline)]
        command = ([sys.executable, "-m", "tests.parallel"] if __spec__ is not None else
                   [sys.executable, os.path.abspath(__file__)]) + tail
        began = time.monotonic()
        problem = None
        timeout_report = ""
        with open(output_file, "w") as output, self.lock:
            if self.stopping:
                return
            load_factor = class_load_factor()
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                           stderr=subprocess.STDOUT, start_new_session=True,
                                           pass_fds=(self.lifeline,), env=env)
            except OSError as error:
                process = None
                problem = f"could not start {batch.identifier}: {error}"
            else:
                self.live[process] = batch.identifier
        if process is not None:
            timeout = stretch * (self.args.timeout if self.args.timeout is not None else batch.timeout)
            deadline = timeout * load_factor
            requested = None
            class_started = time.monotonic()
            remaining = deadline
            try:
                while True:
                    try:
                        process.wait(remaining)
                    except subprocess.TimeoutExpired:
                        new_factor = class_load_factor()
                        if new_factor > load_factor:
                            load_factor = new_factor
                            deadline = timeout * load_factor
                            remaining = max(0, class_started + deadline - time.monotonic())
                            continue
                        problem = f"class exceeded {deadline:g}s"
                        if load_factor > 1:
                            problem += f" (deadline {timeout:g}s x load {load_factor:g})"
                        requested = self.request_stacks(stack_prefix)
                    else:
                        if process.returncode:
                            problem = f"worker exited with status {process.returncode}"
                    break
            finally:
                self.kill(process)
                process.wait()
                with self.lock:
                    self.live.pop(process, None)
            if requested is not None:
                stacks = self.stack_output(requested, process.pid)
                timeout_report = "\n".join(filter(None, (
                    _test_stacks.REPORT_START, batch.identifier, problem, stacks, _test_stacks.REPORT_END,
                )))
        try:
            with open(result_file) as handle:
                report = json.load(handle)
            if (not isinstance(report, dict) or type(report.get("ran")) is not int or report["ran"] < 0
                    or any(not isinstance(report.get(key), list)
                           for key in ("errors", "failures", "skipped", "unexpected"))):
                raise TypeError("invalid report")
            if report["ran"] != batch.count and not (report["errors"] or report["skipped"]):
                problem = problem or f"worker ran {report['ran']} tests; expected {batch.count}"
        except (OSError, ValueError, KeyError, TypeError):
            report = {"ran": 0, "errors": [], "failures": [], "skipped": [], "unexpected": []}
            problem = problem or "worker exited without a complete report"
        if problem and not timeout_report:
            report["errors"].append((batch.identifier, problem))
        report.update(seconds=time.monotonic() - began, output=self.output(output_file), timeout=timeout_report)
        with self.lock:
            self.results[batch.identifier] = report
            if self.args.verbose:
                outcome = "FAIL" if failed(report) else "ok"
                print(f"{batch.identifier}: {report['ran']} tests, {outcome} ({report['seconds']:.2f}s)", flush=True)

    def run(self, tests, named=()):
        batches = {}
        entries = [(test.id().rsplit(".", 1)[0], test) for test in tests]
        for name in named:
            entries += [(name, test) for test in flatten(unittest.defaultTestLoader.loadTestsFromName(name))]
        for identifier, test in entries:
            if isinstance(test, _FailedTest):
                result = unittest.TestResult()
                test.run(result)
                self.results[test.id()] = {"ran": result.testsRun,
                                           "errors": [[str(t), text] for t, text in result.errors],
                                           "failures": [], "skipped": [], "unexpected": [],
                                           "seconds": 0.0, "output": ""}
                continue
            test_class = type(test)
            batch = batches.setdefault(identifier, Batch(
                identifier, 0, serial=getattr(test_class, "serial", False),
                timeout=getattr(test_class, "class_timeout", DEFAULT_CLASS_TIMEOUT)))
            batch.count += 1
        items = list(enumerate(sorted(batches.values(), key=lambda batch: -self.history.get(batch.identifier, 0))))
        began = time.monotonic()
        self.folder = tempfile.mkdtemp(prefix="arete-parallel-")
        self.lifeline, held = os.pipe()
        old_signals = {number: signal.signal(number, self.stop)
                       for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        complete = False
        try:
            with ThreadPoolExecutor(max_workers=self.args.jobs) as pool:
                list(pool.map(self.child, (item for item in items if not item[1].serial)))
            for item in items:
                if item[1].serial:
                    self.child(item)
            self.rerun_failed(items)
            complete = True
        finally:
            if not complete:
                self.stop()
            os.close(self.lifeline)
            os.close(held)
            for number, handler in old_signals.items():
                signal.signal(number, handler)
        if self.stopping:
            print("interrupted; classes in flight: " + ", ".join(self.in_flight), flush=True)
            print(f"Worker reports and output retained in: {self.folder}", flush=True)
            return 130
        return self.finish(time.monotonic() - began)

    def wait_for_load(self, identifier, capacity, budget):
        began = time.monotonic()
        waited = 0
        while True:
            try:
                load = os.getloadavg()[0]
            except OSError:
                load = None
            if (self.stopping or load is None or capacity is None or load <= capacity
                    or waited >= budget):
                break
            if not waited:
                print(f"{identifier}: waiting for host load to fall from {load:g} to {capacity} or less", flush=True)
            time.sleep(min(LOAD_POLL_INTERVAL, budget - waited))
            waited = time.monotonic() - began
        if not self.stopping:
            load_text = f"{load:g}" if load is not None else "unavailable"
            print(f"{identifier}: starting alone at host load {load_text} after waiting {waited:.2f}s", flush=True)
        return load, waited

    def rerun_failed(self, items):
        capacity = os.cpu_count()
        budget = LOAD_WAIT_BUDGET
        for index, batch in items:
            if self.stopping:
                break
            identifier = batch.identifier
            first = self.results.get(identifier, {})
            if not failed(first):
                continue
            self.first_failures[identifier] = first
            load, waited = self.wait_for_load(identifier, capacity, budget)
            budget -= waited
            if self.stopping:
                break
            self.child((f"{index}.alone", batch), stretch=3)
            alone = self.results[identifier]
            if (not self.stopping and alone["timeout"] and load is not None and capacity is not None
                    and load > capacity
                    and not any(alone[kind] for kind in ("failures", "errors", "unexpected"))):
                retry_load, waited = self.wait_for_load(identifier, capacity, budget)
                budget -= waited
                if self.stopping:
                    break
                self.retry_loads[identifier] = (load, retry_load)
                self.child((f"{index}.alone-retry", batch), stretch=3)
                if failed(self.results[identifier]):
                    self.first_solo_timeouts[identifier] = alone
            if not failed(self.results[identifier]):
                self.crowded.append(identifier)

    def show_failures(self, identifier, report):
        timeout_report = report.get("timeout")
        kinds = [kind for kind in ("errors", "failures", "unexpected") if report[kind]]
        if report["output"] and (timeout_report or kinds):
            print(report["output"])
        if timeout_report:
            print(f"\n{timeout_report}")
        for kind in kinds:
            print(f"\n{identifier}: {kind}")
            for entry in report[kind]:
                print(entry if isinstance(entry, str) else "\n".join(entry))

    def finish(self, seconds):
        for identifier in self.crowded:
            report = self.results[identifier]
            first = self.first_failures[identifier]
            where = "ran past its deadline" if first["timeout"] else "failed"
            if identifier in self.retry_loads:
                load, retry_load = self.retry_loads[identifier]
                retry_load_text = f"{retry_load:g}" if retry_load is not None else "unavailable"
                print(f"{identifier}: {where} on its first run, ran past its deadline alone at host load {load:g}, "
                      f"then passed alone at host load {retry_load_text} within three times its deadline "
                      f"in {report['seconds']:.2f}s", flush=True)
                continue
            print(f"{identifier}: {where} on its first run, "
                  f"then passed alone within three times its deadline in {report['seconds']:.2f}s", flush=True)
        failed_classes = []
        ran = skipped = 0
        for identifier, report in self.results.items():
            ran += report["ran"]
            skipped += len(report["skipped"])
            if failed(report):
                failed_classes.append(identifier)
                if (first := self.first_failures.get(identifier)) and first["timeout"]:
                    self.show_failures(identifier, first)
                if first_solo := self.first_solo_timeouts.get(identifier):
                    self.show_failures(identifier, first_solo)
                self.show_failures(identifier, report)
        try:
            with open(f"{HISTORY}.lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    with open(HISTORY) as handle:
                        history = json.load(handle)
                except (OSError, ValueError):
                    history = {}
                if not isinstance(history, dict):
                    history = {}
                history.update({name: report["seconds"] for name, report in self.results.items()})
                temporary = f"{HISTORY}.{os.getpid()}.tmp"
                with open(temporary, "w") as handle:
                    json.dump(history, handle)
                os.replace(temporary, HISTORY)
        except OSError:
            pass
        if self.args.slowest:
            print("Slowest classes:")
            for name, report in sorted(self.results.items(), key=lambda item: -item[1]["seconds"])[:self.args.slowest]:
                print(f"  {report['seconds']:.2f}s {name} ({report['ran']} tests)")
        if failed_classes:
            print(f"Failed classes: {', '.join(sorted(failed_classes))}")
        print(f"Ran {ran} tests in {seconds:.2f}s: {'FAILED' if failed_classes else 'OK'}" +
              (f" (skipped={skipped})" if skipped else ""), flush=True)
        if failed_classes:
            print(f"Worker reports and output retained in: {self.folder}", flush=True)
            return 1
        shutil.rmtree(self.folder)
        return 0 if ran else 5


def main():
    parser = argparse.ArgumentParser(prog="tests.parallel")
    parser.add_argument("-s", "--start", default=TESTS)
    parser.add_argument("-j", "--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=float, help="override the per-class deadline (default: 10 seconds unless declared)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--slowest", type=int, default=0)
    parser.add_argument("--lifeline", type=int)
    parser.add_argument("--one")
    parser.add_argument("--result")
    parser.add_argument("names", nargs="*", help="classes or tests to run by name; takes no check lane")
    args = parser.parse_args()
    args.start = os.path.abspath(args.start)
    if args.one:
        return run_one(args.one, args.start, args.result, args.lifeline)
    if (args.jobs < 1 or args.slowest < 0
            or (args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0))):
        parser.error("jobs and timeout must be positive; slowest must be nonnegative")
    if args.names:
        sys.path.insert(0, args.start)
        return Runner(args).run([], args.names)
    with lanes.held_by_worker(os.environ, lambda note: print(f"tests.parallel: {note}", flush=True)):
        tests = list(flatten(unittest.TestLoader().discover(args.start)))
        return Runner(args).run(tests)


if __name__ == "__main__":
    sys.exit(main())
