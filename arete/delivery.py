from __future__ import annotations

import sqlite3
import threading
import traceback
from contextlib import contextmanager, suppress
from datetime import timedelta

from . import config, state, util
from .emits import Digest, Emit, Metric, Notify, PullRequest, Ticket, ToLoop
from .loops import Loop
from .push import Push

CLAIM_SECONDS = 120
RETRIED = ("screen", "proof")
SAYS_WHY = (RuntimeError, OSError, ValueError)
EMITS: dict[str, Emit] = {"ticket": Ticket(), "pr": PullRequest(), "push": Push(), "loop": ToLoop(),
                          "digest": Digest(), "notify": Notify(), "metric": Metric()}


class Failed(RuntimeError):
    def __init__(self, why: str, cause: Exception, trace: str = ""):
        super().__init__(why)
        self.cause = cause
        self.trace = trace

    def kept_by(self, emit: Emit) -> bool:
        return isinstance(self.cause, emit.kept_failures)

    def log_to(self, log) -> None:
        log("error", str(self))
        if self.trace:
            log("error", self.trace)


class KeptFailure(Failed):
    pass


class AlreadyDecided(ValueError):
    pass


def deliver(loop: Loop, run_id: str, findings: list[dict], outcome: dict, *, trial: bool, log,
            cfg: config.Config) -> None:
    for spec in loop.emits:
        emit = EMITS[spec["type"]]
        for batch in emit.batches(findings, outcome):
            delivery = {"type": spec["type"], "item": emit.render(loop, run_id, spec, batch, outcome), "batch": batch}
            what = emit.describe(delivery["item"])
            if trial:
                log("held", f"would {what}")
            elif why := emit.held(spec, delivery["item"], cfg):
                delivery |= {"held": why, "reviewer": emit.reviewer(spec, delivery["item"], cfg)}
                waiting_id = state.add_approval(loop.name, run_id, delivery, what)
                settle_judged(emit, run_id, "waiting", waiting_id)
                log("emit", f"waiting for approval #{waiting_id} ({why}): {what}")
            else:
                try:
                    done = send(delivery, loop.name, run_id, cfg)
                except Failed as e:
                    if e.kept_by(emit):
                        failed_id = state.add_approval(loop.name, run_id, delivery, what, str(e))
                        settle_judged(emit, run_id, "failed", failed_id)
                        e.log_to(log)
                        log("emit", f"kept as approval #{failed_id} to try again or reject")
                    else:
                        e.log_to(log)
                else:
                    settle_judged(emit, run_id, "landed")
                    emit.landed(batch, loop.name, done)
                    log("emit", f"{spec['type']}: {done}")


def settle_judged(emit: Emit, run_id: str, outcome: str, approval: int | None = None,
                  was: tuple[str, ...] = ("",)) -> None:
    if emit.judged:
        state.settle_verdicts(run_id, outcome, approval, was)


def objections(loop: Loop, run_id: str, outcome: dict, cfg: config.Config) -> dict[str, str]:
    push = EMITS["push"]
    for spec in loop.emits:
        if spec["type"] != "push" or not push.batches([], outcome):
            continue
        item = push.render(loop, run_id, spec, [], outcome)
        if push.ruled(spec, item, cfg):
            continue
        found = push.objections(item, cfg)
        if found and all(kind in RETRIED for kind in found):
            return found
    return {}


def send(delivery: dict, loop_name: str, run_id: str, cfg: config.Config, *, wrap_crashes: bool = False) -> str:
    kind = delivery["type"]
    try:
        return EMITS[kind].execute(delivery["item"], delivery.get("batch") or [], loop_name, run_id, cfg)
    except (*SAYS_WHY, KeyError) as e:
        raise Failed(why_failed(kind, e), e) from e
    except Exception as e:
        if not wrap_crashes:
            raise
        raise Failed(why_failed(kind, e), e, traceback.format_exc().strip()) from e


def why_failed(kind: str, e: Exception) -> str:
    said = util.first_line(str(e), 300)
    if said and isinstance(e, SAYS_WHY):
        return f"{kind} failed: {said}"
    named = type(e).__name__
    return f"{kind} failed: {named}: {said}" if said else f"{kind} failed: {named}"


def waiting(connect=state.db) -> list[dict]:
    return [{"id": a["id"], "loop": a["loop"], "run_id": a["run_id"], "created": a["created"], "repo": repo_of(a),
             "what": a["summary"], "plain_summary": a["plain_summary"],
             "failed": a["result"] if a["status"] == "failed" else "", "held": a["emit"].get("held", ""),
             "review": review_shown(a), "approving": emit_of(a).approving,
             "detail": emit_of(a).detail(a["emit"]["item"])} for a in state.approvals(connect)]


def reviewer_of(approval: dict) -> str:
    return (approval["emit"].get("reviewer") or {}).get("model", "")


def reviewer_may_land(approval: dict) -> bool:
    return bool((approval["emit"].get("reviewer") or {}).get("may_land"))


def review_shown(approval: dict) -> dict:
    if review := approval["review"]:
        kept = f" It waits for you, because {review['kept']}." if review.get("kept") else ""
        return {"by": review["by"],
                "said": f"{review['decision']}, {review['confidence']} confidence. {review['reason']}{kept}"}
    if (model := reviewer_of(approval)) and approval["status"] == "pending":
        return {"by": model, "said": "not read yet."}
    return {}


def emit_of(approval: dict) -> Emit:
    return EMITS[approval["emit"]["type"]]


def repo_for(approval: dict, cfg: config.Config) -> config.Repo | None:
    name = repo_of(approval)
    return next((r for r in cfg.repos if r.name == name), None)


def repo_of(approval: dict) -> str:
    return emit_of(approval).target_repo(approval["emit"]["item"]) or approval["run_repo"]


def cut_off(why: str) -> str:
    return f"the send was cut off ({why}) and may have landed; check before approving again"


def claim_deadline() -> str:
    return util.iso(util.now() + timedelta(seconds=CLAIM_SECONDS))


def already_decided(approval_id: int) -> AlreadyDecided:
    row = state.approval(approval_id)
    if row["status"] == "approved":
        return AlreadyDecided(f"approval #{approval_id} is still sending since {row['decided']}; it returns to the "
                              f"approval list within {CLAIM_SECONDS} seconds of the send stopping")
    return AlreadyDecided(f"approval #{approval_id} is already {row['status']}")


@contextmanager
def renewed(approval_id: int, token: str):
    stopped = threading.Event()

    def renew() -> None:
        while not stopped.wait(CLAIM_SECONDS / 4):
            with suppress(sqlite3.Error):
                state.renew_claim(approval_id, token, claim_deadline())

    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()


def release_lapsed() -> list[int]:
    lapsed = state.lapsed_claims()
    if not lapsed or util.now().timestamp() - util.last_wake() <= CLAIM_SECONDS:
        return []
    released = []
    for claim in lapsed:
        row = state.approval(claim["id"])
        if state.settle_approval(claim["id"], "failed", "failed", cut_off("claim expired"),
                                 judged=emit_of(row).judged, token=claim["claimed_by"], until=claim["claimed_until"]):
            released.append(claim["id"])
    return released


def decide(approval_id: int, yes: bool, by: str = "", came_from: tuple[str, ...] = state.DECIDABLE) -> str:
    row = state.approval(approval_id)
    if not row:
        raise LookupError(f"no approval #{approval_id}")
    delivery, verb = row["emit"], "approved" if yes else "rejected"
    emit = EMITS[delivery["type"]]
    cfg = config.load() if yes else None

    def log(kind: str, text: str) -> None:
        if row["run_id"]:
            state.add_feed(row["run_id"], kind, f"{verb} #{approval_id}{by and f' by {by}'}, {text}")

    if not yes:
        if not state.settle_approval(approval_id, "rejected", "rejected", judged=emit.judged, came_from=came_from):
            raise already_decided(approval_id)
        emit.rejected(delivery["item"])
        log("emit", f"nothing sent: {row['summary']}")
        return "rejected"
    token = state.claim_approval(approval_id, claim_deadline(), came_from)
    if not token:
        raise already_decided(approval_id)

    def settle_as(status: str, outcome: str, why: str) -> bool:
        return state.settle_approval(approval_id, status, outcome, why, judged=emit.judged, token=token)

    with renewed(approval_id, token):
        try:
            done = send(delivery, row["loop"], row["run_id"], cfg, wrap_crashes=True)
            landed = settle_as("done", "approved", done)
        except Failed as e:
            if e.kept_by(emit):
                settle_as("failed", "failed", str(e))
                e.log_to(log)
                raise KeptFailure(str(e), e.cause) from e.cause
            why = f"{e}. Trying again would fail the same way, so it is rejected"
            if settle_as("rejected", "rejected", why):
                emit.rejected(delivery["item"])
                e.log_to(log)
                log("emit", "rejected, because trying again would fail the same way")
            raise Failed(why, e.cause) from e.cause
        except BaseException as e:
            settle_as("failed", "failed", cut_off(type(e).__name__))
            raise
    if not landed:
        raise already_decided(approval_id)
    emit.landed(delivery.get("batch") or [], row["loop"], done)
    log("emit", f"{delivery['type']}: {done}")
    return done
