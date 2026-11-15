from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
from collections.abc import Callable

from . import scratch, util
from .agent import Cut

STOP_GRACE_SECONDS = 30
DRAIN_SECONDS = 10
OVERRUN_SECONDS = STOP_GRACE_SECONDS + DRAIN_SECONDS
ANSWERED_QUIET_SECONDS = 60
SETTLE_POLL_SECONDS = 1


def stream(cmd: list[str], cwd: str | None, prompt: str, env: dict[str, str], timeout: float,
           on_line: Callable[[str], None], *, actor_confined: bool = False) -> subprocess.CompletedProcess:
    child = scratch.spawn(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, errors="replace", env=env, actor_confined=actor_confined)
    out: list[str] = []
    err: list[str] = []
    cuts: list[Cut] = []
    answer = Answer()

    def watched(line: str) -> None:
        if cuts:
            return
        answer.saw(line)
        try:
            on_line(line)
        except Cut as cut:
            cuts.append(cut)
            util.kill_tree(child.pid)

    helpers = [threading.Thread(target=feed, args=(child.stdin, prompt), daemon=True),
               threading.Thread(target=collect, args=(child.stdout, out, watched), daemon=True),
               threading.Thread(target=collect, args=(child.stderr, err, lambda line: None), daemon=True)]
    for helper in helpers:
        helper.start()
    try:
        answered_and_stopped = settle(child, timeout, answer)
    except BaseException:
        stop(child)
        raise
    finally:
        release(child)
        for helper in helpers:
            helper.join(DRAIN_SECONDS)
    if cuts:
        raise cuts[0]
    return subprocess.CompletedProcess(cmd, 0 if answered_and_stopped else child.returncode, "".join(out), "".join(err))


def release(proc: subprocess.Popen) -> None:
    if scratch.PRODUCING.get() is not None:
        from . import verify

        verify.stop(proc)


class Answer:
    def __init__(self) -> None:
        self.given = False
        self.last_line = time.monotonic()

    def saw(self, line: str) -> None:
        self.last_line = time.monotonic()
        self.given = self.given or structured_result(line)

    def quiet_for(self) -> float:
        return time.monotonic() - self.last_line if self.given else 0.0


def structured_result(line: str) -> bool:
    if '"structured_output"' not in line:
        return False
    try:
        event = json.loads(line)
    except ValueError:
        return False
    return isinstance(event, dict) and event.get("type") == "result" and event.get("structured_output") is not None


def settle(child: subprocess.Popen, timeout: float, answer: Answer) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if answer.quiet_for() >= ANSWERED_QUIET_SECONDS or (answer.given and left <= 0):
            stop(child)
            return True
        if left <= 0:
            raise subprocess.TimeoutExpired(child.args, timeout)
        try:
            child.wait(timeout=min(left, SETTLE_POLL_SECONDS))
            return False
        except subprocess.TimeoutExpired:
            continue


def feed(stdin, prompt: str) -> None:
    with contextlib.suppress(OSError, ValueError), stdin:
        stdin.write(prompt)


def collect(stream, lines: list[str], on_line: Callable[[str], None]) -> None:
    with stream:
        for line in stream:
            lines.append(line)
            on_line(line)


def stop(child: subprocess.Popen) -> None:
    child.terminate()
    try:
        child.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        util.kill_tree(child.pid)
        child.wait()
