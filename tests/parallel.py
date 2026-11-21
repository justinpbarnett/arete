import argparse
import fcntl
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from importlib import import_module
from unittest.loader import _FailedTest

TAIL = 4000
DEFAULT_CLASS_TIMEOUT = 10
STACK_GRACE = 1
HISTORY = os.path.join(tempfile.gettempdir(), "arete-parallel-durations.json")
TESTS = os.path.dirname(os.path.abspath(__file__))
STACK_SITE = os.path.join(TESTS, "_parallel_site")
TIMING_LIMIT = 5
TIMING_NAME_BYTES = 8192
TIMING_PACKET_BYTES = 48 * 1024
LONG_TIMING_NAME = "[test name too long]"
_timing_clock = time.monotonic
_timing_seek = os.lseek

if __spec__ is None:
    sys.path.insert(0, os.path.dirname(TESTS))
_test_stacks = import_module("arete._test_stacks")
THREAD = re.compile(r"(?:Current thread|Thread) (0x[0-9a-f]+) \(most recent call first\):")


def stack_env(env):
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


def timing_name(test):
    # Use the built-in descriptors directly so timing cannot run test-defined attribute hooks.
    attributes = unittest.TestCase.__dict__["__dict__"].__get__(test)
    method = None
    for name, value in dict.items(attributes):
        if type(name) is str and name == "_testMethodName":
            method = value
            break
    cls = type(test)
    module = type.__dict__["__module__"].__get__(cls)
    qualname = type.__dict__["__qualname__"].__get__(cls)
    if type(module) is type(qualname) is type(method) is str:
        return f"{module}.{qualname}.{method}"


class TimedResult(unittest.TestResult):
    def __init__(self, record, started_fd=None):
        super().__init__()
        self.record = record
        self.started_fd = started_fd

    def startTest(self, test):
        super().startTest(test)
        self.started = self.name = None
        with suppress(OSError):
            if self.started_fd is not None:
                _timing_seek(self.started_fd, self.testsRun, os.SEEK_SET)
        with suppress(Exception):
            if self.record is not None:
                started = _timing_clock()
                if math.isfinite(started):
                    self.started = started
                    self.name = timing_name(test)

    def stopTest(self, test):
        super().stopTest(test)
        with suppress(Exception):
            if self.started is not None and self.name is not None:
                finished = _timing_clock()
                duration = finished - self.started
                if 0 <= duration < math.inf:
                    self.record(finished, self.name, duration)


def pending_packets(sock, size):
    with suppress(BlockingIOError):
        while packet := sock.recv(size):
            yield packet


class TimingPublisher:
    def __init__(self, writer, reader):
        self.writer = writer
        self.reader = reader
        self.deadline = None
        self.rows = []

    def record(self, finished, name, duration):
        if self.deadline is None:
            with suppress(BlockingIOError):
                self.deadline = float(self.writer.recv(64))
        if self.deadline is not None and finished > self.deadline:
            return
        if len(self.rows) == TIMING_LIMIT and duration <= self.rows[-1][2]:
            return
        if len(name) > TIMING_NAME_BYTES or len(json.dumps(name)) > TIMING_NAME_BYTES:
            name = LONG_TIMING_NAME
        rows = sorted([*self.rows, (finished, name, duration)], key=lambda row: -row[2])[:TIMING_LIMIT]
        packet = json.dumps(rows).encode()
        try:
            self.writer.send(packet)
        except OSError:
            return
        # Publish first so a worker killed during replacement leaves a complete snapshot.
        if self.rows:
            with suppress(BlockingIOError):
                self.reader.recv(1)
        self.rows = rows


@contextmanager
def timing_channel():
    with ExitStack() as cleanup:
        try:
            reader, writer = socket.socketpair(type=socket.SOCK_DGRAM)
            cleanup.enter_context(reader)
            cleanup.enter_context(writer)
            # Reserve space for the old and new snapshots during replacement.
            writer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * TIMING_PACKET_BYTES)
            reader.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * TIMING_PACKET_BYTES)
            reader.setblocking(False)
            writer.setblocking(False)
        except OSError:
            yield None
            return
        yield reader, writer


def timing_rows(reader):
    rows = []
    with suppress(OSError):
        for packet in pending_packets(reader, TIMING_PACKET_BYTES):
            with suppress(ValueError, RecursionError):
                snapshot = json.loads(packet)
                if isinstance(snapshot, list) and len(snapshot) <= TIMING_LIMIT:
                    rows = [row for row in snapshot if isinstance(row, list) and len(row) == 3
                            and isinstance(row[0], (int, float)) and isinstance(row[1], str)
                            and isinstance(row[2], (int, float))]
    return rows


def private_fd(fd):
    if fd is not None:
        try:
            os.set_inheritable(fd, False)
        except OSError:
            return None
    return fd


def run_one(identifier, start, result_file, lifeline, progress_fd=None, progress_peer_fd=None, started_fd=None):
    os.set_inheritable(lifeline, False)
    threading.Thread(target=end_with_runner, args=(lifeline,), daemon=True).start()
    record = None
    with suppress(OSError):
        writer_fd, reader_fd = private_fd(progress_fd), private_fd(progress_peer_fd)
        if writer_fd is not None and reader_fd is not None:
            writer, reader = socket.socket(fileno=writer_fd), socket.socket(fileno=reader_fd)
            record = TimingPublisher(writer, reader).record
    started_fd = private_fd(started_fd)
    result = TimedResult(record, started_fd) if record is not None or started_fd is not None else unittest.TestResult()
    sys.path.insert(0, start)
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


def class_timeout_details(batch, timeout, deadline, events, started):
    timings = [(duration, name) for timestamp, name, duration in events
               if deadline is not None and timestamp <= deadline and isinstance(name, str) and 0 <= duration < math.inf]
    details = {"class_name": batch.identifier, "count": batch.count, "deadline": timeout,
               "timings": sorted(timings, key=lambda entry: -entry[0])[:TIMING_LIMIT]}
    if started is not None and 0 <= started <= batch.count:
        details["never_started"] = batch.count - started
    return details


class Runner:
    def __init__(self, args):
        self.args = args
        self.summary_path = os.environ.get(_test_stacks.SUMMARY_FILE_ENV)
        self.lock = threading.RLock()
        self.live = {}
        self.stopping = False
        self.in_flight = []
        self.results = {}
        self.crowded = []
        self.first_failures = {}
        try:
            with open(HISTORY) as handle:
                self.history = json.load(handle)
        except (OSError, ValueError):
            self.history = {}

    def kill(self, process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self, *args):
        with self.lock:
            self.stopping = True
            self.in_flight = list(self.live.values())
            for process in self.live:
                self.kill(process)

    def output(self, path):
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size <= 2 * TAIL:
                return handle.read().decode(errors="replace")
            head = handle.read(TAIL).decode(errors="replace")
            handle.seek(-TAIL, os.SEEK_END)
            return head + f"\n... full output: {path} ...\n" + handle.read().decode(errors="replace")

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
        env = stack_env({**os.environ, _test_stacks.PREFIX_ENV: os.path.join(self.folder, stack_prefix)})
        env.pop(_test_stacks.SUMMARY_FILE_ENV, None)
        tail = ["--one", batch.identifier, "--start", self.args.start, "--result", result_file,
                "--lifeline", str(self.lifeline)]
        command = ([sys.executable, "-m", "tests.parallel"] if __spec__ is not None else
                   [sys.executable, os.path.abspath(__file__)]) + tail
        began = time.monotonic()
        problem = None
        timeout_report = ""
        deadline = None
        events = ()
        started = started_fd = None
        with ExitStack() as cleanup:
            progress = cleanup.enter_context(timing_channel()) if self.summary_path else None
            pass_fds = (self.lifeline,)
            if progress is not None:
                reader, writer = progress
                command += ["--progress-fd", str(writer.fileno()), "--progress-peer-fd", str(reader.fileno())]
                pass_fds += (writer.fileno(), reader.fileno())
            with open(output_file, "w") as output, self.lock:
                if self.stopping:
                    return
                if self.summary_path:
                    # A separately opened descriptor shares its offset with the worker, leaving stdout untouched.
                    with suppress(OSError):
                        started_fd = os.open(output_file, os.O_RDONLY)
                        cleanup.callback(os.close, started_fd)
                        command += ["--started-fd", str(started_fd)]
                        pass_fds += (started_fd,)
                try:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                               stderr=subprocess.STDOUT, start_new_session=True,
                                               pass_fds=pass_fds, env=env)
                except OSError as error:
                    process = None
                    problem = f"could not start {batch.identifier}: {error}"
                else:
                    self.live[process] = batch.identifier
            if process is not None:
                timeout = stretch * (self.args.timeout if self.args.timeout is not None else batch.timeout)
                requested = None
                if progress is not None:
                    with suppress(Exception):
                        deadline = _timing_clock() + timeout
                        # The worker excludes stack-dump grace time before ranking its five rows.
                        reader.send(str(deadline).encode())
                try:
                    process.wait(timeout)
                    if process.returncode:
                        problem = f"worker exited with status {process.returncode}"
                except subprocess.TimeoutExpired:
                    if started_fd is not None:
                        with suppress(OSError):
                            started = _timing_seek(started_fd, 0, os.SEEK_CUR)
                    problem = f"class exceeded {timeout:g}s"
                    requested = self.request_stacks(stack_prefix)
                finally:
                    self.kill(process)
                    process.wait()
                    with self.lock:
                        self.live.pop(process, None)
                if requested is not None:
                    if progress is not None:
                        events = timing_rows(reader)
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
        if timeout_report and self.summary_path:
            report["timeout_details"] = class_timeout_details(batch, timeout, deadline, events, started)
        with self.lock:
            self.results[batch.identifier] = report
            if self.args.verbose:
                outcome = "FAIL" if failed(report) else "ok"
                print(f"{batch.identifier}: {report['ran']} tests, {outcome} ({report['seconds']:.2f}s)", flush=True)

    def run(self, tests):
        batches = {}
        for test in tests:
            if isinstance(test, _FailedTest):
                result = unittest.TestResult()
                test.run(result)
                self.results[test.id()] = {"ran": result.testsRun,
                                           "errors": [[str(t), text] for t, text in result.errors],
                                           "failures": [], "skipped": [], "unexpected": [],
                                           "seconds": 0.0, "output": ""}
                continue
            identifier = test.id().rsplit(".", 1)[0]
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
            for index, batch in items:
                identifier = batch.identifier
                first = self.results.get(identifier, {})
                if not self.stopping and failed(first):
                    self.first_failures[identifier] = first
                    self.child((f"{index}.alone", batch), stretch=2)
                    if not failed(self.results[identifier]):
                        self.crowded.append(identifier)
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

    def show_failures(self, identifier, report):
        timeout_report = report.get("timeout")
        if timeout_report:
            print(f"\n{timeout_report}")
            if report["output"]:
                print(report["output"])
        for kind in ("errors", "failures", "unexpected"):
            if not report[kind]:
                continue
            print(f"\n{identifier}: {kind}")
            for entry in report[kind]:
                print(entry if isinstance(entry, str) else "\n".join(entry))
            if report["output"] and not timeout_report:
                print(report["output"])

    def finish(self, seconds):
        suite_failed = False
        summaries = []
        ran = skipped = 0
        for identifier, report in self.results.items():
            ran += report["ran"]
            skipped += len(report["skipped"])
            if failed(report):
                suite_failed = True
                if (first := self.first_failures.get(identifier)) and first["timeout"]:
                    self.show_failures(identifier, first)
                self.show_failures(identifier, report)
                if self.summary_path and first and len(summaries) < _test_stacks.MAX_TIMEOUT_SUMMARY_CLASSES:
                    details = report.get("timeout_details") or first.get("timeout_details")
                    if details:
                        summaries.append(details)
        try:
            temporary = f"{HISTORY}.{os.getpid()}.tmp"
            with open(temporary, "w") as handle:
                json.dump({name: report["seconds"] for name, report in self.results.items()}, handle)
            os.replace(temporary, HISTORY)
        except OSError:
            pass
        for identifier in self.crowded:
            report = self.results[identifier]
            first = self.first_failures[identifier]
            where = "ran past its deadline" if first["timeout"] else "failed"
            print(f"{identifier}: {where} on its first run, "
                  f"then passed alone within twice its deadline in {report['seconds']:.2f}s", flush=True)
        if self.args.slowest:
            print("Slowest classes:")
            for name, report in sorted(self.results.items(), key=lambda item: -item[1]["seconds"])[:self.args.slowest]:
                print(f"  {report['seconds']:.2f}s {name} ({report['ran']} tests)")
        print(f"Ran {ran} tests in {seconds:.2f}s: {'FAILED' if suite_failed else 'OK'}" +
              (f" (skipped={skipped})" if skipped else ""), flush=True)
        if suite_failed:
            print(f"Worker reports and output retained in: {self.folder}", flush=True)
            if summaries:
                try:
                    with open(self.summary_path, "ab", buffering=0,
                              opener=lambda path, flags: os.open(path, flags & ~os.O_CREAT)) as sidecar:
                        for summary in summaries:
                            sidecar.write((json.dumps(summary) + "\n").encode())
                except OSError:
                    pass
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
    parser.add_argument("--progress-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--progress-peer-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--started-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.start = os.path.abspath(args.start)
    if args.one:
        return run_one(args.one, args.start, args.result, args.lifeline,
                       args.progress_fd, args.progress_peer_fd, args.started_fd)
    if (args.jobs < 1 or args.slowest < 0
            or (args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0))):
        parser.error("jobs and timeout must be positive; slowest must be nonnegative")
    tests = list(flatten(unittest.TestLoader().discover(args.start)))
    return Runner(args).run(tests)


if __name__ == "__main__":
    sys.exit(main())
