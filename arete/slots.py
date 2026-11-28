from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from typing import TextIO
from urllib.parse import quote
from uuid import uuid4

from . import config, paths, record, refusals, util
from .store import chats as chats_store
from .store import runs as run_store
from .store import zte as zte_store

SLOTS_LOCK = "run-slots"
CHECKS_LOCK = "checks"
CHECK_SLOTS_LOCK = "check-slots"
CHECK_LIMIT_PREFIX = "check-limit-"
SLOT_KINDS = ("pipeline", "experiment")
FREED_SLOT_FOR_CI = "ci: freed the run slot for the wait"
SLOT_POLL_SECONDS = 30
CHECK_SLOT_POLL_SECONDS = 0.25


def _row_holds_lock(row: dict, lock_kind: str) -> bool:
    task_id = row.get("task_id")
    if lock_kind == "pipeline" and row.get("kind") == "pipeline" and row.get("lock"):
        if util.lock_is_held(row["lock"]):
            return True
    if task_id is not None:
        if util.lock_is_held(run_store.run_lock(lock_kind, task=int(task_id))):
            return True
        from .store import tasks as task_store

        if not task_store.is_contracted():
            if util.lock_is_held(run_store.run_lock(lock_kind, row["repo"], row["issue"])):
                return True
        return False
    return util.lock_is_held(run_store.run_lock(lock_kind, row["repo"], row["issue"]))


def holding(rows: list[dict], lock_kind: str, kinds: tuple[str, ...] = ("pipeline",)) -> list[dict]:
    return [r for r in rows if r["kind"] in kinds and _row_holds_lock(r, lock_kind)]


def chat_sessions_going(running: list[dict]) -> list[dict]:
    linked = {link["run_id"] for link in chats_store.chat_sessions()}
    return [r for r in running if r["kind"] == "session" and r["id"] in linked
            and ((util.now() - util.parse_iso(r["started"])).total_seconds() < record.SETTLE_GRACE_SECONDS
                 or (r["lock"] and util.lock_is_held(r["lock"])))]


def runs_in_slots(running: list[dict] | None = None) -> list[dict]:
    running = run_store.runs_marked_running() if running is None else running
    return [*holding(running, "slot", SLOT_KINDS), *chat_sessions_going(running)]


def runs_waiting_for_a_slot() -> list[dict]:
    return holding(run_store.runs_marked_running(), "slot-wait")


def runs_waiting_for_merge_lease() -> list[dict]:
    return holding(run_store.runs_marked_running(), "merge-wait")


def slots_free_to_start(max_parallel: int) -> int:
    if zte_store.activation_pending() or runs_waiting_for_a_slot() or runs_waiting_for_merge_lease():
        return 0
    return max_parallel - len(runs_in_slots())


def freed_slot_for_ci(run_id: str) -> bool:
    log = paths.RUNS_DIR / run_id / "run.log"
    return log.exists() and FREED_SLOT_FOR_CI in log.read_text()


def live_max_parallel(last_read: int) -> int:
    try:
        from . import pins

        return pins.active_config().max_parallel
    except (OSError, ValueError):
        return last_read


@dataclass
class RunSlot:
    repo: str
    tid: str
    max_parallel: int
    task_id: int | None = None
    held: TextIO | None = None
    held_extra: TextIO | None = None

    def hold(self) -> None:
        if self.task_id is not None:
            from .store import tasks as task_store

            if not task_store.is_contracted():
                self.held_extra = hold(self.repo, self.tid)
            self.held = util.hold_lock(run_store.run_lock("slot", task=int(self.task_id)))
        else:
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
                    if not zte_store.activation_pending() and len(runs_in_slots()) < self.max_parallel:
                        self.hold()
                        log(f"{what}: took a run slot back" + (" after waiting for one" if waiting else ""))
                        return
                    if not waiting:
                        waiting = True
                        if self.task_id is not None:
                            from .store import tasks as task_store

                            if not task_store.is_contracted():
                                stack.enter_context(util.hold_lock(run_store.run_lock("slot-wait", self.repo, self.tid)))
                            stack.enter_context(util.hold_lock(run_store.run_lock("slot-wait", task=int(self.task_id))))
                        else:
                            stack.enter_context(util.hold_lock(run_store.run_lock("slot-wait", self.repo, self.tid)))
                        stack.enter_context(waits("slot"))
                        log(f"{what}: waiting for a free run slot (max_parallel {self.max_parallel}), ahead of new runs")
                time.sleep(SLOT_POLL_SECONDS)

    def free(self) -> None:
        if self.held:
            self.held.close()
            self.held = None
        if self.held_extra:
            self.held_extra.close()
            self.held_extra = None


def no_free_slot(max_parallel: int) -> str:
    if zte_store.activation_pending():
        return "a managed release is being verified or restored"
    if waiting := len(runs_waiting_for_a_slot()):
        return (f"{util.count(waiting, 'run')} {'waits' if waiting == 1 else 'wait'} to take a run slot back first "
                f"(max_parallel {max_parallel})")
    if waiting := len(runs_waiting_for_merge_lease()):
        return f"{util.count(waiting, 'run')} {'waits' if waiting == 1 else 'wait'} for an automerge lease first"
    return f"no run slot is free (max_parallel {max_parallel})"


class NoFreeSlot(refusals.Conflict, RuntimeError):
    pass


def admitting(*, wait: bool = True):
    return util.lock(SLOTS_LOCK, wait=wait)


def hold(repo: str, tid: str | None):
    return util.hold_lock(run_store.run_lock("slot", repo, tid))


def live_max_parallel_checks(last_read: int) -> int:
    try:
        value = config.read_data().get("max_parallel_checks", config.Config.max_parallel_checks)
        return max(1, int(value))
    except (OSError, ValueError, TypeError, OverflowError):
        return last_read


def check_lock_file(directory: int, name: str) -> TextIO:
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    file = os.fdopen(fd, "r+")
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
        file.close()
        raise PermissionError("invalid check lock file")
    return file


def check_slot_usage(directory: int, limit: int) -> tuple[set[str], int]:
    occupied = set()
    prefix = f"{CHECKS_LOCK}-"
    for name in os.listdir(directory):
        if name.startswith(CHECK_LIMIT_PREFIX):
            with check_lock_file(directory, name) as request:
                if util.flock_now(request):
                    os.unlink(name, dir_fd=directory)
                else:
                    limit = min(limit, int(request.read()))
        elif name.startswith(prefix) and name.removeprefix(prefix).isdigit():
            with check_lock_file(directory, name) as lease:
                if not util.flock_now(lease):
                    occupied.add(name)
    return occupied, limit


@dataclass
class CheckSlotSeeker:
    locks: ExitStack
    directory: int
    serial: TextIO | None
    request: TextIO | None = None

    def register(self, limit: int) -> None:
        if self.request is None:
            self.request = self.locks.enter_context(
                check_lock_file(self.directory, f"{CHECK_LIMIT_PREFIX}{uuid4().hex}"))
            util.flock_now(self.request)
        self.request.seek(0)
        util.mark_holder(self.request, limit)

    def take_free(self, limit: int) -> tuple[TextIO | None, str]:
        with check_lock_file(self.directory, CHECK_SLOTS_LOCK) as admission:
            if not util.flock_now(admission):
                return None, "another run is admitting checks"
            util.mark_holder(admission, os.getpid())
            self.register(limit)
            occupied, limit = check_slot_usage(self.directory, limit)
            if len(occupied) >= limit:
                return None, f"max_parallel_checks {limit}"
            if self.serial is not None and not util.flock_now(self.serial):
                return None, "serialized checks"
            names = (f"{CHECKS_LOCK}-{i}" for i in range(limit))
            lease = check_lock_file(self.directory, next(name for name in names if name not in occupied))
            if not util.flock_now(lease):
                lease.close()
                return None, f"max_parallel_checks {limit}"
            util.mark_holder(lease, os.getpid())
            if self.serial is not None:
                util.mark_holder(self.serial, os.getpid())
            return self.locks.enter_context(lease), ""


@contextmanager
def check_slot(run, *, parallel: bool):
    with ExitStack() as locks:
        directory = locks.enter_context(paths.directory_fd(paths.CHECK_LOCKS_DIR, create=True))
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("check lock directory must be private to this user")
        limit = live_max_parallel_checks(run.config.max_parallel_checks)
        serial = None if parallel else locks.enter_context(check_lock_file(directory, CHECKS_LOCK))
        seeker = CheckSlotSeeker(locks, directory, serial)
        lease, reason = seeker.take_free(limit)
        if lease is None:
            with run.waiting("slot"):
                run.log(f"checks: waiting for a check slot ({reason})")
                while lease is None:
                    time.sleep(CHECK_SLOT_POLL_SECONDS)
                    limit = live_max_parallel_checks(limit)
                    lease, _ = seeker.take_free(limit)
        yield tuple(lock.fileno() for lock in (lease, seeker.request, serial) if lock is not None)


def merge_lock(github: str) -> str:
    return f"automerge-{quote(github.casefold(), safe='')}"


def take_merge_lease(run, github: str, *, slot: RunSlot | None = None) -> TextIO:
    lease = util.lock_file(merge_lock(github))
    try:
        with ExitStack() as stack:
            with admitting():
                if util.flock_now(lease):
                    util.mark_holder(lease, os.getpid())
                    return lease
                task_id = getattr(run, "task_id", None)
                if isinstance(task_id, int) and not isinstance(task_id, bool):
                    from .store import tasks as task_store

                    if not task_store.is_contracted():
                        stack.enter_context(util.hold_lock(run_store.run_lock("merge-wait", run.repo.name, run.tid)))
                    stack.enter_context(util.hold_lock(run_store.run_lock("merge-wait", task=task_id)))
                else:
                    stack.enter_context(util.hold_lock(run_store.run_lock("merge-wait", run.repo.name, run.tid)))
                resume = slot if slot is not None and slot.held else None
                if resume is not None:
                    resume.free()
            with run.waiting("slot"):
                run.log(f"automerge: waiting for the merge lease for {github}, ahead of new runs")
                util.take_lock(lease, wait=True)
            if resume is not None:
                resume.take(run.log, run.waiting, "automerge")
        return lease
    except BaseException:
        lease.close()
        raise
