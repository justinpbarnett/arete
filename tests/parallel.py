import argparse
import json
import math
import os
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
from unittest.loader import _FailedTest

TAIL = 4000
DEFAULT_CLASS_TIMEOUT = 10
HISTORY = os.path.join(tempfile.gettempdir(), "arete-parallel-durations.json")


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
        try:
            with open(HISTORY) as handle:
                self.history = json.load(handle)
        except (OSError, ValueError):
            self.history = {}

    def kill(self, process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
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

    def child(self, item):
        index, batch = item
        result_file = os.path.join(self.folder, f"{index}.json")
        output_file = os.path.join(self.folder, f"{index}.out")
        tail = ["--one", batch.identifier, "--start", self.args.start, "--result", result_file,
                "--lifeline", str(self.lifeline)]
        command = ([sys.executable, "-m", "tests.parallel"] if __spec__ is not None else
                   [sys.executable, os.path.abspath(__file__)]) + tail
        began = time.monotonic()
        problem = None
        with open(output_file, "w") as output, self.lock:
            if self.stopping:
                return
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                           stderr=subprocess.STDOUT, start_new_session=True,
                                           pass_fds=(self.lifeline,))
            except OSError as error:
                process = None
                problem = f"could not start {batch.identifier}: {error}"
            else:
                self.live[process] = batch.identifier
        if process is not None:
            timeout = self.args.timeout if self.args.timeout is not None else batch.timeout
            try:
                process.wait(timeout)
                if process.returncode:
                    problem = f"worker exited with status {process.returncode}"
            except subprocess.TimeoutExpired:
                problem = f"class exceeded {timeout:g}s"
            finally:
                self.kill(process)
                process.wait()
                with self.lock:
                    self.live.pop(process, None)
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
        if problem:
            report["errors"].append((batch.identifier, problem))
        report.update(seconds=time.monotonic() - began, output=self.output(output_file))
        with self.lock:
            self.results[batch.identifier] = report
            if self.args.verbose:
                outcome = "FAIL" if report["errors"] or report["failures"] or report["unexpected"] else "ok"
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

    def finish(self, seconds):
        failed = False
        ran = skipped = 0
        for identifier, report in self.results.items():
            ran += report["ran"]
            skipped += len(report["skipped"])
            for kind in ("errors", "failures", "unexpected"):
                if not report[kind]:
                    continue
                failed = True
                print(f"\n{identifier}: {kind}")
                for entry in report[kind]:
                    print(entry if isinstance(entry, str) else "\n".join(entry))
                if report["output"]:
                    print(report["output"])
        try:
            temporary = f"{HISTORY}.{os.getpid()}.tmp"
            with open(temporary, "w") as handle:
                json.dump({name: report["seconds"] for name, report in self.results.items()}, handle)
            os.replace(temporary, HISTORY)
        except OSError:
            pass
        if self.args.slowest:
            print("Slowest classes:")
            for name, report in sorted(self.results.items(), key=lambda item: -item[1]["seconds"])[:self.args.slowest]:
                print(f"  {report['seconds']:.2f}s {name} ({report['ran']} tests)")
        print(f"Ran {ran} tests in {seconds:.2f}s: {'FAILED' if failed else 'OK'}" +
              (f" (skipped={skipped})" if skipped else ""), flush=True)
        if failed:
            print(f"Worker reports and output retained in: {self.folder}", flush=True)
            return 1
        shutil.rmtree(self.folder)
        return 0 if ran else 5


def main():
    parser = argparse.ArgumentParser(prog="tests.parallel")
    parser.add_argument("-s", "--start", default=os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("-j", "--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=float, help="override the per-class deadline (default: 10 seconds unless declared)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--slowest", type=int, default=0)
    parser.add_argument("--lifeline", type=int)
    parser.add_argument("--one")
    parser.add_argument("--result")
    args = parser.parse_args()
    args.start = os.path.abspath(args.start)
    if args.one:
        return run_one(args.one, args.start, args.result, args.lifeline)
    if (args.jobs < 1 or args.slowest < 0
            or (args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0))):
        parser.error("jobs and timeout must be positive; slowest must be nonnegative")
    tests = list(flatten(unittest.TestLoader().discover(args.start)))
    return Runner(args).run(tests)


if __name__ == "__main__":
    sys.exit(main())
