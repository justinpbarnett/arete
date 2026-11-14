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
        temporary = f"{HISTORY}.{os.getpid()}.tmp"
        with open(temporary, "w") as handle:
            json.dump(history, handle)
        os.replace(temporary, HISTORY)
    except OSError:
        pass


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


def run_one(identifier, start, result_file, lifeline=None):
    if lifeline is not None:
        os.set_inheritable(lifeline, False)
        threading.Thread(target=end_with_runner, args=(lifeline,), daemon=True).start()
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
    def __init__(self, start, jobs, timeout, verbose, slowest=0):
        self.start, self.jobs, self.timeout, self.verbose = start, jobs, timeout, verbose
        self.slowest = slowest
        self.lock = threading.Lock()
        self.live = {}
        self.stopping = False
        self.in_flight = None
        self.lifeline = self.held = None
        self.ran = 0
        self.errors, self.failures = [], []
        self.skipped, self.expected, self.unexpected = 0, 0, []
        self.folder = None
        self.history = load_history()
        self.seen = {}

    def command(self, identifier, result_file):
        tail = ["--one", identifier, "--start", self.start, "--result", result_file,
                "--lifeline", str(self.lifeline)]
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
        with open(output_file, "w") as output:
            with self.lock:
                if self.stopping:
                    return None
                try:
                    proc = subprocess.Popen(
                        self.command(identifier, result_file), stdin=subprocess.DEVNULL, stdout=output,
                        stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(self.lifeline,))
                except OSError as error:
                    proc = None
                    problem = f"could not start a worker for {identifier}: {error}"
                else:
                    self.live[proc] = str(test)
            if proc is not None:
                try:
                    proc.wait(self.timeout)
                except subprocess.TimeoutExpired:
                    self.kill(proc)
                    proc.wait()
                    problem = f"{identifier} was killed after {self.timeout:g} seconds without finishing"
                finally:
                    self.kill(proc)
                    with self.lock:
                        self.live.pop(proc, None)
                if self.stopping:
                    return None
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
            self.unexpected += report["unexpected"]
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

    def stop(self):
        with self.lock:
            self.stopping = True
            if self.in_flight is None:
                self.in_flight = sorted(self.live.values())
            for proc in list(self.live):
                self.kill(proc)

    def interrupt(self, number, frame):
        self.stop()

    def phase(self, items, workers):
        with ThreadPoolExecutor(max_workers=workers) as executor:
            try:
                list(executor.map(self.task, items))
            except BaseException:
                self.stop()
                raise

    def run(self, tests):
        broken = [test for test in tests if isinstance(test, _FailedTest)]
        rest = [test for test in tests if not isinstance(test, _FailedTest)]
        pool = [item for item in enumerate(rest) if not getattr(type(item[1]), "serial", False)]
        alone = [item for item in enumerate(rest) if getattr(type(item[1]), "serial", False)]
        pool.sort(key=lambda item: (not getattr(type(item[1]), "slow", False), -self.history.get(item[1].id(), 0)))
        began = time.monotonic()
        self.lifeline, self.held = os.pipe()
        try:
            with tempfile.TemporaryDirectory(prefix="arete-parallel-") as folder:
                self.folder = folder
                for test in broken:
                    self.broken(test)
                for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    signal.signal(number, self.interrupt)
                self.phase(pool, self.jobs)
                self.phase(alone, 1)
        finally:
            os.close(self.lifeline)
            os.close(self.held)
        if self.stopping:
            print("interrupted; tests in flight: " + (", ".join(self.in_flight or []) or "none"), flush=True)
            return 130
        save_history({**self.history, **self.seen})
        return self.finish(time.monotonic() - began)

    def slowest_block(self):
        classes = {}
        for identifier, seconds in self.seen.items():
            classes.setdefault(identifier.rpartition(".")[0], []).append((seconds, identifier))
        ranked = sorted(classes.items(), key=lambda row: -sum(second for second, _ in row[1]))
        print("Slowest classes:")
        for name, rows in ranked[:self.slowest]:
            longest = max(rows)
            print(f"  {sum(second for second, _ in rows):.1f}s {name} ({len(rows)} tests); "
                  f"longest {longest[1].rpartition('.')[2]} {longest[0]:.1f}s")

    def finish(self, seconds):
        for kind, rows in (("ERROR", self.errors), ("FAIL", self.failures)):
            for title, text, tail in rows:
                print(f"{RULE}\n{kind}: {title}\n{DASH}\n{text.rstrip()}")
                if tail.strip():
                    print(f"Output:\n{tail.rstrip()}")
        for title in self.unexpected:
            print(f"{RULE}\nUNEXPECTED SUCCESS: {title}")
        if self.slowest:
            self.slowest_block()
        bad = bool(self.errors or self.failures or self.unexpected)
        counts = [("failures", len(self.failures)), ("errors", len(self.errors)), ("skipped", self.skipped),
                  ("expected failures", self.expected), ("unexpected successes", len(self.unexpected))]
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
    parser.add_argument("--slowest", type=int, default=0)
    parser.add_argument("--lifeline", type=int)
    parser.add_argument("--one")
    parser.add_argument("--result")
    args = parser.parse_args()
    start = os.path.abspath(args.start)
    if args.one:
        return run_one(args.one, start, args.result, args.lifeline)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.timeout <= 0:
        parser.error("--timeout must be above 0")
    if args.slowest < 0:
        parser.error("--slowest must not be negative")
    tests = list(flatten(unittest.TestLoader().discover(start)))
    return Runner(start, args.jobs, args.timeout, args.verbose, args.slowest).run(tests)


if __name__ == "__main__":
    sys.exit(main())
