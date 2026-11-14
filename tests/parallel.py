import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.loader import _FailedTest

TAIL = 4000
RULE = "=" * 70
DASH = "-" * 70
HISTORY = os.path.join(tempfile.gettempdir(), "arete-parallel-durations.json")


def load_history():
    try:
        with open(HISTORY) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_history(history):
    try:
        with open(HISTORY + ".tmp", "w") as handle:
            json.dump(history, handle)
        os.replace(HISTORY + ".tmp", HISTORY)
    except OSError:
        pass


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def run_one(identifier, start, result_file):
    if start not in sys.path:
        sys.path.insert(0, start)
    result = unittest.TestResult()
    result.startTestRun()
    unittest.defaultTestLoader.loadTestsFromName(identifier).run(result)
    result.stopTestRun()
    report = {
        "ran": result.testsRun,
        "errors": [[str(test), text] for test, text in result.errors],
        "failures": [[str(test), text] for test, text in result.failures],
        "skipped": [[str(test), reason] for test, reason in result.skipped],
        "expected": len(result.expectedFailures),
        "unexpected": [str(test) for test in result.unexpectedSuccesses],
    }
    temporary = result_file + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(report, handle)
    os.replace(temporary, result_file)
    return 0


class Runner:
    def __init__(self, start, jobs, timeout, verbose):
        self.start, self.jobs, self.timeout, self.verbose = start, jobs, timeout, verbose
        self.lock = threading.Lock()
        self.live = set()
        self.running = {}
        self.stopping = False
        self.ran = 0
        self.errors, self.failures = [], []
        self.skipped, self.expected, self.unexpected = 0, 0, 0
        self.folder = None
        self.history = load_history()
        self.seen = {}

    def command(self, identifier, result_file):
        tail = ["--one", identifier, "--start", self.start, "--result", result_file]
        if __spec__ is not None:
            return [sys.executable, "-m", "tests.parallel", *tail]
        return [sys.executable, os.path.abspath(__file__), *tail]

    def kill(self, proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def captured(self, path):
        try:
            with open(path, errors="replace") as handle:
                return handle.read()[-TAIL:]
        except OSError:
            return ""

    def child(self, test, index):
        identifier = test.id()
        result_file = os.path.join(self.folder, f"{index}.json")
        output_file = os.path.join(self.folder, f"{index}.out")
        report, problem = None, None
        with self.lock:
            if self.stopping:
                return None
        with open(output_file, "w") as output:
            proc = subprocess.Popen(
                self.command(identifier, result_file), stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.STDOUT, start_new_session=True)
            with self.lock:
                self.live.add(proc)
                self.running[proc.pid] = str(test)
            try:
                proc.wait(self.timeout)
            except subprocess.TimeoutExpired:
                self.kill(proc)
                proc.wait()
                problem = f"{identifier} was killed after {self.timeout:g} seconds without finishing"
            finally:
                with self.lock:
                    self.live.discard(proc)
                    self.running.pop(proc.pid, None)
        tail = self.captured(output_file)
        if problem is None:
            try:
                with open(result_file) as handle:
                    report = json.load(handle)
            except (OSError, ValueError):
                problem = f"worker exited with status {proc.returncode} before reporting {identifier}"
        if report is None:
            report = {"ran": 1, "errors": [[str(test), problem]], "failures": [], "skipped": [],
                      "expected": 0, "unexpected": []}
        report["tail"] = tail
        return report

    def record(self, test, report, seconds):
        with self.lock:
            self.ran += report["ran"]
            self.errors += [(title, text, report["tail"]) for title, text in report["errors"]]
            self.failures += [(title, text, report["tail"]) for title, text in report["failures"]]
            self.skipped += len(report["skipped"])
            self.expected += report["expected"]
            self.unexpected += len(report["unexpected"])
            if not self.verbose:
                return
            if report["errors"]:
                outcome = "ERROR"
            elif report["failures"]:
                outcome = "FAIL"
            elif report["skipped"]:
                outcome = f"skipped {report['skipped'][0][1]!r}"
            elif report["expected"]:
                outcome = "expected failure"
            elif report["unexpected"]:
                outcome = "unexpected success"
            else:
                outcome = "ok"
            print(f"{test} ... {outcome} ({seconds:.1f}s)", flush=True)

    def task(self, item):
        index, test = item
        began = time.monotonic()
        report = self.child(test, index)
        if report is not None:
            seconds = time.monotonic() - began
            self.seen[test.id()] = seconds
            self.record(test, report, seconds)

    def broken(self, test):
        result = unittest.TestResult()
        test.run(result)
        report = {"ran": result.testsRun, "errors": [[str(t), text] for t, text in result.errors],
                  "failures": [], "skipped": [], "expected": 0, "unexpected": [], "tail": ""}
        self.record(test, report, 0.0)

    def interrupt(self, number, frame):
        with self.lock:
            self.stopping = True
            names = sorted(self.running.values())
            for proc in list(self.live):
                self.kill(proc)
        print("interrupted; tests in flight: " + (", ".join(names) or "none"), flush=True)
        os._exit(130)

    def run(self, tests):
        broken = [test for test in tests if isinstance(test, _FailedTest)]
        rest = [test for test in tests if not isinstance(test, _FailedTest)]
        pool = [item for item in enumerate(rest) if not getattr(type(item[1]), "serial", False)]
        alone = [item for item in enumerate(rest) if getattr(type(item[1]), "serial", False)]
        pool.sort(key=lambda item: (not getattr(type(item[1]), "slow", False), -self.history.get(item[1].id(), 0)))
        signal.signal(signal.SIGINT, self.interrupt)
        signal.signal(signal.SIGTERM, self.interrupt)
        began = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="arete-parallel-") as folder:
            self.folder = folder
            for test in broken:
                self.broken(test)
            with ThreadPoolExecutor(max_workers=self.jobs) as executor:
                list(executor.map(self.task, pool))
            for item in alone:
                self.task(item)
        save_history({**self.history, **self.seen})
        return self.finish(time.monotonic() - began)

    def finish(self, seconds):
        for kind, rows in (("ERROR", self.errors), ("FAIL", self.failures)):
            for title, text, tail in rows:
                print(f"{RULE}\n{kind}: {title}\n{DASH}\n{text.rstrip()}")
                if tail.strip():
                    print(f"Output:\n{tail.rstrip()}")
        bad = bool(self.errors or self.failures or self.unexpected)
        counts = [("failures", len(self.failures)), ("errors", len(self.errors)), ("skipped", self.skipped),
                  ("expected failures", self.expected), ("unexpected successes", self.unexpected)]
        detail = ", ".join(f"{name}={count}" for name, count in counts if count)
        line = f"Ran {self.ran} tests in {seconds:.1f}s: {'FAILED' if bad else 'OK'}"
        print(f"{line} ({detail})" if detail else line, flush=True)
        if bad:
            return 1
        return 0 if self.ran else 5


def main():
    parser = argparse.ArgumentParser(prog="tests.parallel")
    parser.add_argument("-s", "--start", default=os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("-j", "--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--one")
    parser.add_argument("--result")
    args = parser.parse_args()
    start = os.path.abspath(args.start)
    if args.one:
        return run_one(args.one, start, args.result)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.timeout <= 0:
        parser.error("--timeout must be above 0")
    tests = list(flatten(unittest.TestLoader().discover(start)))
    return Runner(start, args.jobs, args.timeout, args.verbose).run(tests)


if __name__ == "__main__":
    sys.exit(main())
