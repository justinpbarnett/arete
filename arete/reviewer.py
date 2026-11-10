from __future__ import annotations

import json
import secrets
from pathlib import Path

from . import config, delivery, outbound, paths, pipeline, runner, state, triage, util

SCHEMA = {"type": "object", "required": ["decision", "confidence", "reason"], "properties": {
    "decision": {"type": "string", "enum": ["approve", "reject"]},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    "reason": {"type": "string"}}}
GIT_READS = ["git log*", "git show*", "git diff*", "git blame*", "git grep*"]
TIMEOUT_SECONDS = 900
BRIEFING_SHOWN = 20000
DIFF_SHOWN = 60000
COMMITS_SHOWN = 12
LONGEST_REASON = 600


class NoReview(RuntimeError):
    pass


def wanted(approval: dict) -> bool:
    return approval["status"] == "pending" and bool(delivery.reviewer_of(approval))


def review(approval: dict, cfg: config.Config) -> str:
    item, model = approval["emit"]["item"], delivery.reviewer_of(approval)
    src, base = Path(item["workdir"]), item["base"]
    wt = paths.WORKTREES_DIR / f"review-{approval['id']}"
    util.git(["fetch", "-q", "origin", base], src)
    util.git(["worktree", "remove", "--force", str(wt)], src, check=False)
    util.git(["worktree", "add", "-q", "--detach", str(wt), item["branch"]], src)
    try:
        try:
            util.git([*util.commit_identity(wt), "rebase", "-q", f"origin/{base}"], wt)
        except RuntimeError as e:
            clashes = util.git_records(["diff", "--name-only", "--diff-filter=U"], wt, check=False)
            util.git(["rebase", "--abort"], wt, check=False)
            found = no_longer_rebases(base, clashes, e)
        else:
            found = read_change(approval, wt, base, model, cfg)
    finally:
        util.git(["worktree", "remove", "--force", str(wt)], src, check=False)
    state.set_approval_note(approval["id"], "review", json.dumps(found))
    said = f"{found['by']} says {found['decision']} with {found['confidence']} confidence"
    if not (found["decision"] == "approve" and found["confidence"] == "high"):
        return f"{said}, so it waits for you"
    if not approval["emit"]["reviewer"]["may_land"]:
        return f"{said}, and it waits for you because {approval['emit']['held']}"
    try:
        return f"{said}, so it lands: {delivery.decide(approval['id'], True, by=f'the {model} reviewer')}"
    except ValueError:
        return f"{said}, but a person decided it first"


def no_longer_rebases(base: str, clashes: list[str], e: RuntimeError) -> dict:
    where = f"newer commits there also change {', '.join(clashes[:5])}" if clashes else util.first_line(str(e), 300)
    return {"decision": "reject", "confidence": "high", "by": "git", "at": util.iso(),
            "reason": f"The change no longer rebases onto origin/{base}, because {where}. "
                      "It cannot land as it is, so reject it and let the loop try again on the new code."}


def read_change(approval: dict, wt: Path, base: str, model: str, cfg: config.Config) -> dict:
    emit, item = delivery.emit_of(approval), approval["emit"]["item"]
    files = util.git_records(["diff", "--name-only", "--no-renames", f"origin/{base}", "HEAD"], wt)
    diff = util.git(["diff", "--no-renames", f"origin/{base}", "HEAD"], wt)
    log = util.git(["log", f"-n{COMMITS_SHOWN}", "--date=short", "--format=%h %ad %s", f"origin/{base}", "--", *files],
                   wt) if files else ""
    briefing_without_patch = emit.briefing({**item, "patch": ""})
    text = pipeline.prompt("reviewer", loop=approval["loop"], repo=delivery.repo_of(approval) or "none", base=base,
                           held=approval["emit"]["held"], mark=secrets.token_hex(8),
                           briefing=briefing_without_patch[:BRIEFING_SHOWN], diff=shown_diff(diff, base),
                           log=log.strip() or "none")
    res = runner.run(text, wt, model=model, allowed_tools=runner.READ_TOOLS + pipeline.bash_rules(GIT_READS),
                     mcp_config=triage.NO_MCP_SERVERS, json_schema=SCHEMA, persist=False, timeout=TIMEOUT_SECONDS,
                     wait_out_limits=False, trace=(approval["run_id"], f"review #{approval['id']}"), cfg=cfg)
    said = res.structured if res.ok and isinstance(res.structured, dict) else {}
    reason = outbound.plain(str(said.get("reason") or ""), LONGEST_REASON).strip()
    if said.get("decision") not in ("approve", "reject") or said.get("confidence") not in ("high", "medium", "low") \
            or not reason:
        raise NoReview(res.error or f"the answer held no decision: {util.first_line(json.dumps(said), 300)}")
    return {"decision": said["decision"], "confidence": said["confidence"], "reason": reason, "by": model,
            "at": util.iso()}


def shown_diff(diff: str, base: str) -> str:
    if len(diff) <= DIFF_SHOWN:
        return diff
    return f"{diff[:DIFF_SHOWN]}\n\ncut at {DIFF_SHOWN} characters; git diff origin/{base} HEAD shows the rest"
