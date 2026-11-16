from __future__ import annotations


class Refusal(Exception):
    status: int | None = None


class Invalid(Refusal):
    status = 400


class NotFound(Refusal):
    status = 404


class Conflict(Refusal):
    status = 409


class TooLarge(Refusal):
    status = 413


class Upstream(Refusal):
    status = 502


class Stated(Refusal):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


class TrackerFailed(Upstream, RuntimeError):
    pass
