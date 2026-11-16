from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum

from . import config, jev

PASSED, FLAGGED, UNSCREENED, FAILED = "passed", "flagged", "unscreened", "failed"


class Posture(Enum):
    OPEN = "open"
    WHEN_ON = "when on"
    REQUIRED = "required"


@dataclass(frozen=True)
class Screen:
    status: str
    p: float | None = None
    line: float = 0.0
    answer: jev.Answer | None = field(default=None, compare=False, repr=False)
    reply: jev.Reply | None = field(default=None, compare=False, repr=False)


def held(screen: Screen, posture: Posture) -> bool:
    if screen.status == FLAGGED:
        return True
    if screen.status == UNSCREENED:
        return posture is Posture.REQUIRED
    if screen.status == FAILED:
        return posture is not Posture.OPEN
    return False


def of_answer(answer: jev.Answer | None, line: float, reply: jev.Reply | None = None) -> Screen:
    if answer is None:
        return Screen(FAILED, None, line, None, reply)
    return Screen(FLAGGED if answer.yes >= line else PASSED, answer.yes, line, answer, reply)


def question() -> dict[str, dict]:
    return {"injection": jev.yes_no(jev.INJECTION)}


def judged(reply: jev.Reply, line: float) -> Screen:
    return of_answer(reply.answers.get("injection"), line, reply)


def fits(state: str | dict | list) -> bool:
    text = state if isinstance(state, str) else json.dumps(state, default=str)
    return len(text) <= jev.STATE_CHARS


def screen(text: str | dict | list, cfg: config.Config, repos: list[config.Repo | None],
           trace: tuple[str, str] = ("", ""), *, ready: bool | None = None) -> Screen:
    line = cfg.jev.injection
    if not (jev.ready(cfg, repos) if ready is None else ready):
        return Screen(UNSCREENED, None, line)
    return judged(jev.ask(text, question(), cfg, trace=trace), line)


def screen_many(items: list[str | dict | list], cfg: config.Config, repos: list[config.Repo | None],
                trace: tuple[str, str] = ("", ""), *, whole: bool = True, ready: bool | None = None) -> list[Screen]:
    line = cfg.jev.injection
    if not (jev.ready(cfg, repos) if ready is None else ready):
        return [Screen(UNSCREENED, None, line) for _ in items]
    sendable = [fits(item) if whole else True for item in items]
    sent = [item for item, ok in zip(items, sendable, strict=True) if ok]
    replies = iter(jev.ask_many([(item, question()) for item in sent], cfg, trace=trace) if sent else [])
    return [judged(next(replies), line) if ok else Screen(FAILED, None, line)
            for ok in sendable]
