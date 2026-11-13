from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config, delivery, outbound, pipeline, push, runner, state, util

DECISIONS = ("approve", "reject")
CONFIDENCES = ("high", "medium", "low")
SCHEMA = {"type": "object", "required": ["decision", "confidence", "reason"], "properties": {
    "decision": {"type": "string", "enum": list(DECISIONS)},
    "confidence": {"type": "string", "enum": list(CONFIDENCES)},
    "reason": {"type": "string"}}}
GIT_READS = ["git log", "git log *", "git show", "git show *", "git diff", "git diff *", "git blame *"]
GIT_READS_OUTSIDE_THE_WORKTREE = ["git * --no-index*", "git * --contents*", "git * --ext-diff*"]
STILL_PENDING = ("pending",)
TIMEOUT_SECONDS = 900
BRIEFING_SHOWN = 20000
DIFF_SHOWN = 60000
COMMITS_SHOWN = 12
LONGEST_REASON = 600


class NoReview(RuntimeError):
    pass


@dataclass(frozen=True)
class Review:
    decision: str
    confidence: str
    reason: str
    by: str
    at: str = field(default_factory=util.iso)
    kept: str = ""

    def sure_approve(self) -> bool:
        return self.decision == "approve" and self.confidence == "high"

    def said(self) -> str:
        return f"{self.by} says {self.decision} with {self.confidence} confidence"


REVIEW_TOOLS = runner.READ_TOOLS + pipeline.bash_rules(GIT_READS)
REVIEW_DENIED = pipeline.bash_rules(GIT_READS_OUTSIDE_THE_WORKTREE)


def model_for(approval: dict, cfg: config.Config) -> str:
    return delivery.reviewer_of(approval) if approval["status"] == "pending" else ""


def review(approval: dict, cfg: config.Config) -> str:
    item, model = approval["emit"]["item"], delivery.reviewer_of(approval)
    src, base = Path(item["workdir"]), item["base"]
    util.git(["fetch", "-q", "origin", base], src)
    with push.branch_worktree(src, item["branch"], f"review-{approval['id']}") as wt:
        try:
            push.rebase_onto(wt, base)
        except push.NoLongerRebases as e:
            if not e.clashes:
                raise NoReview(str(e)) from e
            found, kept = git_reject(e), ""
        else:
            diff = util.git(["diff", "--no-renames", f"origin/{base}", "HEAD"], wt)
            found, kept = read_change(approval, wt, base, diff, model, cfg), why_kept(approval, diff)
    return settle(approval, found, kept)


def why_kept(approval: dict, diff: str) -> str:
    if not delivery.reviewer_may_land(approval):
        return "only a person may clear its hold"
    if len(diff) > DIFF_SHOWN:
        return f"its diff runs past the {DIFF_SHOWN} characters the reviewer reads"
    return ""


def settle(approval: dict, found: Review, kept: str) -> str:
    sure = found.sure_approve()
    noted = Review(**{**asdict(found), "kept": kept if sure else ""})
    if not state.set_approval_note(approval["id"], "review", json.dumps(asdict(noted)), while_in=STILL_PENDING):
        return f"{found.said()}, but a person decided it first"
    if not sure:
        return f"{found.said()}, so it waits for you"
    if kept:
        return f"{found.said()}, but it waits for you, because {kept}"
    try:
        landed = delivery.decide(approval["id"], True, by=f"the {found.by} reviewer", came_from=STILL_PENDING)
    except delivery.AlreadyDecided:
        return f"{found.said()}, but a person decided it first"
    except Exception as e:
        return f"{found.said()}, but it did not land: {util.first_line(str(e), 300)}"
    return f"{found.said()}, so it lands: {landed}"


def git_reject(e: push.NoLongerRebases) -> Review:
    why = str(e)
    return Review("reject", "high", f"{why[:1].upper()}{why[1:]}. "
                  "It cannot land as it is, so reject it and let the loop try again on the new code.", by="git")


def read_change(approval: dict, wt: Path, base: str, diff: str, model: str, cfg: config.Config) -> Review:
    emit, item = delivery.emit_of(approval), approval["emit"]["item"]
    files = util.git_records(["diff", "--name-only", "--no-renames", f"origin/{base}", "HEAD"], wt)
    log = util.git(["log", f"-n{COMMITS_SHOWN}", "--date=short", "--format=%h %ad %s", f"origin/{base}", "--", *files],
                   wt) if files else ""
    briefing_without_patch = emit.briefing({**item, "patch": ""})
    text = pipeline.prompt("hold_review", loop=approval["loop"], repo=delivery.repo_of(approval) or "none", base=base,
                           held=approval["emit"]["held"], mark=secrets.token_hex(8),
                           briefing=briefing_without_patch[:BRIEFING_SHOWN],
                           diff=push.shown_patch(diff, f"git diff origin/{base} HEAD", DIFF_SHOWN),
                           log=log.strip() or "none")
    res = runner.run(text, wt, model=model, allowed_tools=REVIEW_TOOLS, disallowed_tools=REVIEW_DENIED, ask_jev=False,
                     json_schema=SCHEMA, persist=False, timeout=TIMEOUT_SECONDS,
                     wait_out_limits=False, trace=(approval["run_id"], f"review #{approval['id']}"), cfg=cfg,
                     repo=delivery.repo_for(approval, cfg))
    said = res.structured if res.ok and isinstance(res.structured, dict) else {}
    reason = outbound.plain(str(said.get("reason") or ""), LONGEST_REASON).strip()
    if said.get("decision") not in DECISIONS or said.get("confidence") not in CONFIDENCES or not reason:
        raise NoReview(res.error or f"the answer held no decision: {util.first_line(json.dumps(said), 300)}")
    return Review(said["decision"], said["confidence"], reason, by=model)
