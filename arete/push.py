from __future__ import annotations

from pathlib import Path

from . import config, diffguard, jev, outbound, paths, state, util
from .emits import Emit, config_repo
from .loops import Loop, as_list, blank, check_env, worktree_kinds

APPROVE_KEYS = ("lines", "deletes", "paths", "reviewer")
PATCH_SHOWN = 12000


class Push(Emit):
    kept_when_failed = True
    judged = True

    def problems(self, spec: dict, loop: Loop, cfg: config.Config) -> list[str]:
        if loop.kind not in worktree_kinds():
            return [f"only {' and '.join(worktree_kinds())} loops emit a push"]
        if not push_check(spec, loop):
            return [f"a {loop.kind} loop's push emit needs check, the command that must pass on the rebased tree "
                    "before the push lands"]
        approve = spec.get("approve", False)
        if isinstance(approve, dict) and (unknown := sorted(set(approve) - set(APPROVE_KEYS))):
            return [f"a push emit's approve table takes {', '.join(APPROVE_KEYS)}, not {', '.join(unknown)}"]
        if not isinstance(approve, bool | dict):
            return ["a push emit's approve is true, false, or a table of lines, deletes, paths and reviewer"]
        rules = approve if isinstance(approve, dict) else {}
        lines, globs, reviewer = rules.get("lines", 0), rules.get("paths", []), rules.get("reviewer", "opus")
        if isinstance(lines, bool) or not isinstance(lines, int):
            return [f"a push emit's approve lines is a whole number of lines, such as 80, not {lines!r}"]
        if not isinstance(globs, str | list) or not all(isinstance(g, str) for g in globs):
            return [f"a push emit's approve paths is a list of path patterns, such as [\"src/auth/*\"], not {globs!r}"]
        if not isinstance(reviewer, str) or blank(reviewer):
            return [f"a push emit's approve reviewer is the model that reviews a held push, such as \"opus\", "
                    f"not {reviewer!r}"]
        return []

    def batches(self, findings: list[dict], outcome: dict) -> list[list[dict]]:
        return [findings] if outcome.get("branch") else []

    def render(self, loop: Loop, run_id: str, spec: dict, findings: list[dict], outcome: dict) -> dict:
        src = Path(outcome["workdir"])
        start, branch = outcome["start"], outcome["branch"]
        diff = diffguard.between(src, start, branch)
        deleted = util.git_records(["diff", "--name-only", "--no-renames", "--diff-filter=D", start, branch], src)
        public = outcome.get("public", True)
        return {"repo": loop.repo, "workdir": str(src), "branch": branch, "base": outcome["base"],
                "check": push_check(spec, loop), "env": check_env(loop, run_id),
                "timeout": loop.timeout_minutes * 60, "lines": diff.lines, "files": diff.files, "deleted": deleted,
                "title": outbound.plain(f"{loop.name}: run {run_id}" if public else outcome.get("title", ""), 120),
                "report": outbound.plain(outcome.get("facts", "") if public else outcome.get("report", ""), 6000),
                "about": outbound.plain(outcome.get("title", ""), 120), "story": outbound.plain(outcome.get("report", ""), 6000),
                "patch": shown_patch(util.git(["diff", "--no-renames", start, branch], src), start, branch),
                "proof": outcome.get("proof")}

    def ruled(self, spec: dict, item: dict, cfg: config.Config) -> str:
        return self.only_a_person(spec, item, cfg) or self.over_limits(spec, item, cfg)

    def only_a_person(self, spec: dict, item: dict, cfg: config.Config) -> str:
        repo = config_repo(cfg, item["repo"])
        layer = config.load_layer(repo.layer_dir if repo else Path(item["workdir"]) / ".agents")
        if layer.trust_level != "automerge":
            return f"the trust level of {item['repo']} is pr, so a person lands each push"
        if denied := [f for f in item["files"] if util.matches_any(f, diffguard.denied_paths(layer))]:
            return f"touches {', '.join(denied[:5])}, which only a person may change"
        approve = spec.get("approve", False)
        if approve is True:
            return "approve is on"
        rules = approve if isinstance(approve, dict) else {}
        if touched := [f for f in item["files"] if util.matches_any(f, as_list(rules.get("paths")))]:
            return f"touches {', '.join(touched[:5])}, which a person reads"
        return ""

    def over_limits(self, spec: dict, item: dict, cfg: config.Config) -> str:
        approve = spec.get("approve", False)
        rules = approve if isinstance(approve, dict) else {}
        if (limit := rules.get("lines")) and item["lines"] > int(limit):
            return f"{item['lines']} lines changed, over the {limit} line approval limit"
        if rules.get("deletes") and item["deleted"]:
            return f"deletes {', '.join(item['deleted'][:5])}"
        if jev.ready(cfg, [config_repo(cfg, item["repo"])]) and len(item.get("patch", "")) > PATCH_SHOWN:
            return f"the diff runs past the {PATCH_SHOWN} characters JEV reads, so a person reads it whole"
        return ""

    def held(self, spec: dict, item: dict, cfg: config.Config) -> str:
        return self.ruled(spec, item, cfg) or ". ".join(self.objections(item, cfg).values())

    def reviewer(self, spec: dict, item: dict, cfg: config.Config) -> dict:
        approve = spec.get("approve", False)
        if not (isinstance(approve, dict) and (model := approve.get("reviewer"))):
            return {}
        return {"model": model, "may_land": not self.only_a_person(spec, item, cfg)}

    def objections(self, item: dict, cfg: config.Config) -> dict[str, str]:
        if not jev.ready(cfg, [config_repo(cfg, item["repo"])]):
            return {}
        run_id, about = item.get("env", {}).get("ARETE_RUN_ID", ""), item.get("about") or item["title"]
        found = {}
        try:
            if held := jev.screen(item.get("patch", ""), about, cfg, trace=(run_id, "push screen jev")):
                found["screen"] = f"JEV holds it: {'; '.join(held)}"
        except jev.Unjudged as e:
            found["unjudged screen"] = f"JEV could not judge the diff, so a person decides: {e}"
        if proof := item.get("proof"):
            found |= proof_objection(proof, item.get("patch", ""), about, cfg, run_id)
        return found

    def execute(self, item: dict, batch: list[dict], loop_name: str, run_id: str, cfg: config.Config) -> str:
        src, base = Path(item["workdir"]), item["base"]
        if "origin" not in util.git(["remote"], src, check=False).split():
            raise RuntimeError(f"{paths.short_path(src)} has no origin to push to")
        wt = paths.WORKTREES_DIR / f"push-{run_id}"
        util.git(["worktree", "remove", "--force", str(wt)], src, check=False)
        util.git(["worktree", "add", "-q", "--detach", str(wt), item["branch"]], src)
        try:
            sha = land(item, wt, src, base)
        finally:
            util.git(["worktree", "remove", "--force", str(wt)], src, check=False)
        state.set_verdict_sha(run_id, sha)
        util.git(["branch", "-D", item["branch"]], src, check=False)
        return f"pushed {sha[:10]} to {base}; {fast_forward(src, base, sha)}"

    def describe(self, item: dict) -> str:
        return (f"push {item['branch']} to {item['base']} of {item['repo']} after `{item['check']}` passes "
                f"({util.count(item['lines'], 'line')} in {util.count(len(item['files']), 'file')})")

    def detail(self, item: dict) -> str:
        proof = item.get("proof") or {}
        shown = f"Proof, {proof['name']}:\n{proof.get('text') or 'missing from the scratch folder'}" if proof else ""
        return "\n\n".join(filter(None, [item.get("about") or item["title"], f"Files: {', '.join(item['files'])}",
                                          item.get("story") or item["report"], item.get("patch", ""), shown]))

    def briefing(self, item: dict) -> str:
        problem = (item.get("proof") or {}).get("problem")
        return "\n\n".join(filter(None, [f"The problem it fixes:\n{problem}" if problem else "", self.detail(item)]))

    def rejected(self, item: dict) -> None:
        util.git(["branch", "-D", item["branch"]], Path(item["workdir"]), check=False)


def push_check(spec: dict, loop: Loop) -> str:
    return next((check for check in (spec.get("check"), loop.spec.get("check")) if not blank(check)), "")


def proof_objection(proof: dict, patch: str, about: str, cfg: config.Config, run_id: str) -> dict[str, str]:
    name, text = proof["name"], proof.get("text") or ""
    if not text.strip():
        return {"proof": f"the scratch folder holds no {name}, so nothing shows the fix works"}
    if len(text) > PATCH_SHOWN:
        return {"proof": f"{name} runs past the {PATCH_SHOWN} characters JEV reads"}
    try:
        doubts = jev.proof(proof.get("problem") or about, name, text, patch, about, cfg,
                           trace=(run_id, "proof check jev"))
    except jev.Unjudged as e:
        return {"unjudged proof": f"JEV could not judge {name}, so a person decides: {e}"}
    return {"proof": f"JEV doubts that {name} proves the problem: {'; '.join(doubts)}"} if doubts else {}


def shown_patch(patch: str, start: str, branch: str) -> str:
    patch = outbound.redact(patch)
    if len(patch) <= PATCH_SHOWN:
        return patch
    return f"{patch[:PATCH_SHOWN]}\n\ncut at {PATCH_SHOWN} characters; git diff {start[:10]} {branch} shows the rest"


def land(item: dict, wt: Path, src: Path, base: str) -> str:
    for attempt in (1, 2):
        util.git(["fetch", "-q", "origin", base], src)
        util.git(["reset", "-q", "--hard"], wt)
        util.git(["clean", "-qfd"], wt)
        try:
            util.git([*util.commit_identity(wt), "rebase", "-q", f"origin/{base}"], wt)
        except RuntimeError as e:
            util.git(["rebase", "--abort"], wt, check=False)
            raise RuntimeError(f"the change does not rebase onto origin/{base}: {e}") from e
        gate = util.sh(item["check"], cwd=wt, timeout=item["timeout"], env=item["env"])
        if gate.returncode != 0:
            raise RuntimeError(f"`{item['check']}` exits {gate.returncode} after the rebase: "
                               f"{util.last_line((gate.stdout or '') + (gate.stderr or ''), 300)}")
        try:
            util.git(["push", "-q", "origin", f"HEAD:refs/heads/{base}"], wt)
        except RuntimeError:
            if attempt == 2:
                raise
            continue
        util.git(["fetch", "-q", "origin", base], src, check=False)
        return util.git(["rev-parse", "HEAD"], wt).strip()
    raise RuntimeError("unreachable")


def fast_forward(src: Path, base: str, sha: str) -> str:
    on = util.git(["rev-parse", "--abbrev-ref", "HEAD"], src, check=False).strip()
    if on != base:
        return f"{paths.short_path(src)} is on {on}, not {base}, so it was left alone"
    try:
        util.git(["merge", "-q", "--ff-only", sha], src)
    except RuntimeError as e:
        return f"{paths.short_path(src)} did not fast forward: {e}"
    return f"{paths.short_path(src)} fast forwarded"
