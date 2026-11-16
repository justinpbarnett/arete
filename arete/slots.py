from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack, nullcontext
from dataclasses import dataclass
from typing import TextIO

from . import config, paths, record, refusals, state, util

SLOTS_LOCK = "run-slots"
SLOT_KINDS = ("pipeline", "experiment")
FREED_SLOT_FOR_CI = "ci: freed the run slot for the wait"
SLOT_POLL_SECONDS = 30


def holding(rows: list[dict], lock_kind: str, kinds: tuple[str, ...] = ("pipeline",)) -> list[dict]:
    return [r for r in rows
            if r["kind"] in kinds and util.lock_is_held(state.run_lock(lock_kind, r["repo"], r["issue"]))]


def chat_sessions_going(running: list[dict]) -> list[dict]:
    linked = {link["run_id"] for link in state.chat_sessions()}
    return [r for r in running if r["kind"] == "session" and r["id"] in linked
            and ((util.now() - util.parse_iso(r["started"])).total_seconds() < record.SETTLE_GRACE_SECONDS
                 or (r["lock"] and util.lock_is_held(r["lock"])))]


def runs_in_slots(running: list[dict] | None = None) -> list[dict]:
    running = state.runs_marked_running() if running is None else running
    return [*holding(running, "slot", SLOT_KINDS), *chat_sessions_going(running)]


def runs_waiting_for_a_slot() -> list[dict]:
    return holding(state.runs_marked_running(), "slot-wait")


def slots_free_to_start(max_parallel: int) -> int:
    if runs_waiting_for_a_slot():
        return 0
    return max_parallel - len(runs_in_slots())


def freed_slot_for_ci(run_id: str) -> bool:
    log = paths.RUNS_DIR / run_id / "run.log"
    return log.exists() and FREED_SLOT_FOR_CI in log.read_text()


def live_max_parallel(last_read: int) -> int:
    try:
        return config.load().max_parallel
    except (OSError, ValueError):
        return last_read


@dataclass
class RunSlot:
    repo: str
    tid: str
    max_parallel: int
    held: TextIO | None = None

    def hold(self) -> None:
        self.held = hold(self.repo, self.tid)

    def take(self, log: Callable[[str], None],
             waits: Callable[[str], AbstractContextManager] = lambda cause: nullcontext(), what: str = "ci") -> None:
        if self.held:
            return
        waiting = False
        with ExitStack() as stack:
            while True:
                with admitting():
                    self.max_parallel = live_max_parallel(self.max_parallel)
                    if len(runs_in_slots()) < self.max_parallel:
                        self.hold()
                        log(f"{what}: took a run slot back" + (" after waiting for one" if waiting else ""))
                        return
                    if not waiting:
                        waiting = True
                        stack.enter_context(util.hold_lock(state.run_lock("slot-wait", self.repo, self.tid)))
                        stack.enter_context(waits("slot"))
                        log(f"{what}: waiting for a free run slot (max_parallel {self.max_parallel}), ahead of new runs")
                time.sleep(SLOT_POLL_SECONDS)

    def free(self) -> None:
        if self.held:
            self.held.close()
            self.held = None


def no_free_slot(max_parallel: int) -> str:
    if waiting := len(runs_waiting_for_a_slot()):
        return (f"{util.count(waiting, 'run')} {'waits' if waiting == 1 else 'wait'} to take a run slot back first "
                f"(max_parallel {max_parallel})")
    return f"no run slot is free (max_parallel {max_parallel})"


class NoFreeSlot(refusals.Conflict, RuntimeError):
    pass


def admitting():
    return util.lock(SLOTS_LOCK, wait=True)


def hold(repo: str, tid: str | None):
    return util.hold_lock(state.run_lock("slot", repo, tid))
