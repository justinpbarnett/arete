from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import TextIO

from . import paths, util

CHECKS_LOCK = "checks"
ENV = "ARETE_CHECK_LANES"
LANE_NAME = re.compile(rf"{CHECKS_LOCK}(-\d+)?")
WORKER_POLL_SECONDS = 1


def names(parallel: bool, width: int) -> list[str]:
    if not parallel:
        return [CHECKS_LOCK]
    return [f"{CHECKS_LOCK}-{n}" for n in range(width)]


def lock_path(name: str) -> Path:
    return paths.LOCKS_DIR / f"{name}.lock"


def take_free(files: list[TextIO]) -> bool:
    for f in files:
        if util.flock_now(f):
            with suppress(OSError):
                util.mark_holder(f, os.getpid())
            return True
    return False


def worker_names(env: Mapping[str, str]) -> list[str]:
    return [name for name in env.get(ENV, "").split(",") if LANE_NAME.fullmatch(name)]


def worker_lock_paths(env: Mapping[str, str]) -> tuple[Path, ...]:
    return tuple(lock_path(name) for name in worker_names(env))


def open_for_worker(name: str) -> TextIO:
    try:
        return open(lock_path(name), "a")
    except OSError:
        return open(lock_path(name))


def open_all(lane_names: list[str], stack: ExitStack) -> list[TextIO] | None:
    try:
        return [stack.enter_context(open_for_worker(name)) for name in lane_names]
    except OSError:
        return None


@contextmanager
def held_by_worker(env: Mapping[str, str], say: Callable[[str], None]):
    lane_names = worker_names(env)
    with ExitStack() as stack:
        files = open_all(lane_names, stack) if lane_names else None
        if lane_names and files is None:
            say("the check lanes cannot be opened from here, so the full suite runs without taking one")
        elif files and not take_free(files):
            say(f"waiting for a free check lane ({len(files)} in all) before the full suite starts; "
                "this is a queue behind other runs' checks, not a hang")
            while not take_free(files):
                time.sleep(WORKER_POLL_SECONDS)
        yield
