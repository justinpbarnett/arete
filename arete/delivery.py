from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import traceback
from contextlib import contextmanager, suppress
from datetime import timedelta

from . import config, contract, factory_control, gh, jira, sources, util
from .emits import Emit, PullRequest, Ticket, ToLoop
from .loops import Loop
from .push import Push
from .store import approvals as approvals_store
from .store import db as foundation
from .store import insights as insights_store
from .store import outbox as outbox_store
from .store import runs as run_store

CLAIM_SECONDS = 120
RETRIED = ("screen", "proof")
SAYS_WHY = (RuntimeError, OSError, ValueError)
EMITS: dict[str, Emit] = {"ticket": Ticket(), "pr": PullRequest(), "push": Push(), "loop": ToLoop()}


class Failed(RuntimeError):
    def __init__(self, why: str, cause: Exception, trace: str = ""):
        super().__init__(why)
        self.cause = cause
        self.trace = trace

    @property
    def upstream(self) -> bool:
        return isinstance(self.cause, (gh.GhError, jira.JiraError))

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


def _emit_slot(loop_name: str, run_id: str, kind: str, spec_idx: int, batch: list[dict]) -> str:
    if kind == "loop" and batch:
        fp = batch[0].get("fingerprint_key") or batch[0].get("fingerprint") or ""
        digest = hashlib.sha256(json.dumps([loop_name, fp, spec_idx]).encode()).hexdigest()[:24]
        return f"loop:{digest}"
    if kind == "ticket" and batch:
        fp = batch[0].get("fingerprint_key") or batch[0].get("fingerprint") or ""
        digest = hashlib.sha256(json.dumps([loop_name, fp, spec_idx]).encode()).hexdigest()[:24]
        return f"finding:{digest}"
    digest = hashlib.sha256(json.dumps([loop_name, run_id, kind, spec_idx]).encode()).hexdigest()[:24]
    return f"emit:{kind}:{digest}"


def _draft_emit(
    c,
    *,
    loop: Loop,
    run_id: str,
    spec: dict,
    spec_idx: int,
    emit: Emit,
    item: dict,
    batch: list[dict],
    what: str,
    why: str,
    cfg: config.Config,
) -> int:
    from . import outbox

    kind = spec["type"]
    slot = _emit_slot(loop.name, run_id, kind, spec_idx, batch)
    drafted_by = f"loop:{loop.name}"

    if kind == "push":
        return outbox.draft(
            c,
            target="git",
            kind="push",
            object=f"git:{item['repo']}",
            body=json.dumps(item),
            drafted_by=drafted_by,
            slot=slot,
            run_id=run_id,
            loop=loop.name,
            summary=what,
            review=why,
            hold_as_draft=bool(why),
            on_conflict_ignore=True,
        )
    if kind == "pr":
        return outbox.draft(
            c,
            target="github",
            kind="pr",
            object=f"github:{item['repo']}",
            body=json.dumps(item),
            drafted_by=drafted_by,
            slot=slot,
            run_id=run_id,
            loop=loop.name,
            summary=what,
            review=why,
            hold_as_draft=bool(why),
            on_conflict_ignore=True,
        )
    if kind == "loop":
        payload = {"loop": item["loop"], "from_loop": loop.name, "event": item["event"]}
        return outbox.draft(
            c,
            target="loop",
            kind="loop",
            object=f"loop:{item['loop']}",
            body=json.dumps(payload),
            drafted_by=drafted_by,
            slot=slot,
            run_id=run_id,
            loop=loop.name,
            summary=what,
            review=why,
            hold_as_draft=bool(why),
            on_conflict_ignore=True,
        )
    # ticket emit
    dest = item.get("destination") or {}
    if dest.get("tracker") == "jira":
        target_name = "jira"
        obj = f"jira:{dest.get('project', '')}"
        outbox_kind = "issue"
    else:
        target_name = "github"
        github_repo = dest.get("github") or sources.github_name(cfg, item["repo"])
        obj = f"github:{github_repo}"
        outbox_kind = "issue"
    fp = (batch[0].get("fingerprint_key") or batch[0].get("fingerprint") or "") if batch else ""
    payload_ticket: dict[str, object] = {
        "title": item["title"],
        "body": item["body"],
        "labels": list(item["labels"]),
    }
    if dest:
        payload_ticket["destination"] = dict(dest)
    if item.get("issue_type"):
        payload_ticket["issue_type"] = str(item["issue_type"])
    if fp:
        payload_ticket["finding"] = {"loop": loop.name, "fingerprint": fp}
    return outbox.draft(
        c,
        target=target_name,
        kind=outbox_kind,
        object=obj,
        body=json.dumps(payload_ticket),
        drafted_by=drafted_by,
        slot=slot,
        run_id=run_id,
        loop=loop.name,
        summary=what,
        review=why,
        hold_as_draft=bool(why),
        on_conflict_ignore=True,
    )


def deliver(loop: Loop, run_id: str, findings: list[dict], outcome: dict, *, trial: bool, log,
            cfg: config.Config) -> None:
    for spec_idx, spec in enumerate(loop.emits):
        if not trial and spec["type"] == "ticket" and spec.get("tracker", "github") == "github":
            continue
        emit = EMITS[spec["type"]]
        for batch in emit.batches(findings, outcome):
            target = emit.resolve(spec, batch, outcome, cfg) if isinstance(emit, Ticket) else spec
            item = emit.render(loop, run_id, target, batch, outcome)
            what = emit.describe(item)
            if trial:
                log("held", f"would {what}")
                continue
            why = emit.held(spec, item, cfg, run_id=run_id) if isinstance(emit, Push) else emit.held(spec, item, cfg)
            with foundation.tx() as c:
                item_id = _draft_emit(
                    c,
                    loop=loop,
                    run_id=run_id,
                    spec=spec,
                    spec_idx=spec_idx,
                    emit=emit,
                    item=item,
                    batch=batch,
                    what=what,
                    why=why,
                    cfg=cfg,
                )
                if why and emit.judged:
                    insights_store.settle_verdicts(run_id, "waiting", item_id, c=c)
            if why:
                log("emit", f"waiting for approval OB-{item_id} ({why}): {what}")
            else:
                log("emit", f"drafted OB-{item_id}: {what}")


def settle_judged(emit: Emit, run_id: str, outcome: str, approval: int | None = None,
                  was: tuple[str, ...] = ("",)) -> None:
    if emit.judged:
        insights_store.settle_verdicts(run_id, outcome, approval, was)


def objections(loop: Loop, run_id: str, outcome: dict, cfg: config.Config) -> dict[str, str]:
    push = EMITS["push"]
    for spec in loop.emits:
        if spec["type"] != "push" or not push.batches([], outcome):
            continue
        item = push.render(loop, run_id, spec, [], outcome)
        if push.ruled(spec, item, cfg):
            continue
        found = push.objections(item, cfg, run_id=run_id)
        if found and all(kind in RETRIED for kind in found):
            return found
    return {}


@factory_control.work
def send(delivery: dict, loop_name: str, run_id: str, cfg: config.Config, *, wrap_crashes: bool = False,
         publication=None, approval_id: int | None = None, approver: str = "") -> str:
    kind = delivery["type"]
    try:
        args = delivery["item"], delivery.get("batch") or [], loop_name, run_id, cfg
        emit = EMITS[kind]
        if isinstance(emit, Ticket):
            return emit.execute(*args, publication=publication, approval_id=approval_id, approver=approver)
        return emit.execute(*args, publication=publication) if publication else emit.execute(*args)
    except (*SAYS_WHY, KeyError) as e:
        raise Failed(why_failed(kind, e), e) from e
    except Exception as e:
        if not wrap_crashes:
            raise
        raise Failed(why_failed(kind, e), e, traceback.format_exc().strip()) from e


def why_failed(kind: str, e: Exception) -> str:
    if isinstance(e, (gh.GhError, jira.JiraError)):
        return f"{kind} failed: the external tracker did not confirm the request"
    said = util.first_line(str(e), 300)
    if said and isinstance(e, SAYS_WHY):
        return f"{kind} failed: {said}"
    named = type(e).__name__
    return f"{kind} failed: {named}: {said}" if said else f"{kind} failed: {named}"


def waiting(connect=foundation.db) -> list[dict]:
    return [{"id": a["id"], "loop": a["loop"], "run_id": a["run_id"], "created": a["created"], "repo": repo_of(a),
             "what": a["summary"], "plain_summary": a["plain_summary"],
             "failed": a["result"] if a["status"] == "failed" else "", "held": a["emit"].get("held", ""),
             "review": review_shown(a), "approving": emit_of(a).approving,
             "detail": emit_of(a).detail(a["emit"]["item"])} for a in approvals_store.approvals(connect)]


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
    emit = approval.get("emit") or {}
    kind = emit.get("type")
    if kind in EMITS:
        return EMITS[kind]
    if "action" in emit:
        return EMITS["pr"]
    if "event" in emit and "loop" in emit:
        return EMITS["loop"]
    if "title" in emit and "labels" in emit and "branch" not in emit:
        return EMITS["ticket"]
    return EMITS["push"]


def repo_for(approval: dict, cfg: config.Config) -> config.Repo | None:
    name = repo_of(approval)
    return next((r for r in cfg.repos if r.name == name), None)


def repo_of(approval: dict) -> str:
    emit = approval.get("emit") or {}
    item = emit.get("item") if isinstance(emit.get("item"), dict) else emit
    return emit_of(approval).target_repo(item) or approval.get("run_repo", "")


def cut_off(why: str) -> str:
    return f"the send was cut off ({why}) and may have landed; check before approving again"


def claim_deadline() -> str:
    return util.iso(util.now() + timedelta(seconds=CLAIM_SECONDS))


def already_decided(approval_id: int) -> AlreadyDecided:
    row = approvals_store.approval(approval_id)
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
                approvals_store.renew_claim(approval_id, token, claim_deadline())

    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()


def release_lapsed() -> list[int]:
    lapsed = approvals_store.lapsed_claims()
    if not lapsed or util.now().timestamp() - util.last_wake() <= CLAIM_SECONDS:
        return []
    released = []
    for claim in lapsed:
        row = approvals_store.approval(claim["id"])
        if approvals_store.settle_approval(claim["id"], "failed", "failed", cut_off("claim expired"),
                                 judged=emit_of(row).judged, token=claim["claimed_by"], until=claim["claimed_until"]):
            released.append(claim["id"])
    return released


def decide(approval_id: int, yes: bool, by: str = "", came_from: tuple[str, ...] = approvals_store.DECIDABLE,
           *, publication=None) -> str:
    from . import outbox

    pending = (publication.pending or {}) if publication else {}
    if "result" in pending:
        return pending["result"]
    with foundation.reader() as c:
        row = outbox_store.get_by_legacy_approval(c, approval_id) or outbox_store.get_row(c, approval_id)
    if row is None:
        raise LookupError(f"no approval #{approval_id}")
    verb = "approved" if yes else "rejected"
    ob_id = int(row["id"])

    def log(kind: str, text: str) -> None:
        if row.get("run_id") and run_store.run_get(row["run_id"]):
            run_store.add_feed(row["run_id"], kind, f"{verb} #{approval_id}{by and f' by {by}'}, {text}")

    try:
        if not yes:
            outbox.reject(ob_id, reason=by)
            log("emit", f"nothing sent: {row['summary']}")
            if publication:
                with foundation.tx():
                    publication.record(f"approval:{approval_id}", "rejected", "rejected")
            return "rejected"
        if row["state"] == "failed":
            updated = outbox.retry(ob_id)
        else:
            if row["audience"] != "self" and row["opened_at"] is None:
                outbox.open(ob_id)
            updated = outbox.approve(ob_id, approved_by=by or None)
    except contract.Problem as error:
        if error.code == "invalid_state":
            raise AlreadyDecided(str(error)) from None
        raise
    done = str(updated["id"])
    if publication:
        with foundation.tx():
            publication.record(f"approval:{approval_id}", done, done)
    log("emit", f"{row['kind']}: {done}")
    return done
