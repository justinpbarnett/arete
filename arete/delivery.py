from __future__ import annotations

from . import config, state, util
from .emits import Digest, Emit, Metric, Notify, PullRequest, Ticket, ToLoop
from .loops import Loop
from .push import Push

RETRIED = ("screen", "proof")
EMITS: dict[str, Emit] = {"ticket": Ticket(), "pr": PullRequest(), "push": Push(), "loop": ToLoop(),
                          "digest": Digest(), "notify": Notify(), "metric": Metric()}


class Failed(RuntimeError):
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
                    send(delivery, loop.name, run_id, cfg, log)
                    settle_judged(emit, run_id, "landed")
                except Failed as e:
                    if emit.kept_when_failed:
                        failed_id = state.add_approval(loop.name, run_id, delivery, what, str(e))
                        settle_judged(emit, run_id, "failed", failed_id)
                        log("emit", f"kept as approval #{failed_id} to try again or reject")


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


def send(delivery: dict, loop_name: str, run_id: str, cfg: config.Config, log) -> str:
    kind = delivery["type"]
    try:
        done = EMITS[kind].execute(delivery["item"], delivery.get("batch") or [], loop_name, run_id, cfg)
    except (RuntimeError, OSError, ValueError, KeyError) as e:
        why = f"{kind} failed: {util.first_line(str(e), 300)}"
        log("error", why)
        raise Failed(why) from e
    log("emit", f"{kind}: {done}")
    return done


def waiting(connect=state.db) -> list[dict]:
    return [{"id": a["id"], "loop": a["loop"], "run_id": a["run_id"], "created": a["created"], "repo": repo_of(a),
             "what": a["summary"], "plain_summary": a["plain_summary"],
             "failed": a["result"] if a["status"] == "failed" else "", "held": a["emit"].get("held", ""),
             "reviewer": reviewer_of(a), "review": a["review"],
             "detail": emit_of(a).detail(a["emit"]["item"])} for a in state.approvals(connect)]


def reviewer_of(approval: dict) -> str:
    return (approval["emit"].get("reviewer") or {}).get("model", "")


def emit_of(approval: dict) -> Emit:
    return EMITS[approval["emit"]["type"]]


def repo_of(approval: dict) -> str:
    return emit_of(approval).target_repo(approval["emit"]["item"]) or approval["run_repo"]


def decide(approval_id: int, yes: bool, by: str = "") -> str:
    row = state.approval(approval_id)
    if not row:
        raise LookupError(f"no approval #{approval_id}")
    delivery, verb = row["emit"], "approved" if yes else "rejected"
    emit = EMITS[delivery["type"]]
    cfg = config.load() if yes else None
    if not state.decide_approval(approval_id, verb):
        raise ValueError(f"approval #{approval_id} is already {state.approval(approval_id)['status']}")

    def log(kind: str, text: str) -> None:
        if row["run_id"]:
            state.add_feed(row["run_id"], kind, f"{verb} #{approval_id}{by and f' by {by}'}, {text}")

    settle_judged(emit, row["run_id"], verb, approval_id, ("waiting", "failed", "approved"))
    if not yes:
        emit.rejected(delivery["item"])
        log("emit", f"nothing sent: {row['summary']}")
        return "rejected"
    try:
        done = send(delivery, row["loop"], row["run_id"], cfg, log)
    except Exception as e:
        state.decide_approval(approval_id, "failed", str(e))
        settle_judged(emit, row["run_id"], "failed", approval_id, ("approved",))
        raise
    state.decide_approval(approval_id, "done", done)
    return done
