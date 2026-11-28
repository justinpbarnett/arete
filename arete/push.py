from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import config, diffguard, jev, outbound, paths, util, vault
from .emits import Emit, config_repo
from .loops import Loop, as_list, blank, worktree_kinds

APPROVE_KEYS = ("lines", "deletes", "paths", "reviewer")
PATCH_SHOWN = 12000


class Push(Emit):
    judged = True
    approving = "A push rebases and runs its check again before it lands, so it can take a minute."

    def problems(self, spec: dict, loop: Loop, cfg: config.Config) -> list[str]:
        if loop.kind not in worktree_kinds():
            return [f"only {' and '.join(worktree_kinds())} loops emit a push"]
        if not push_check(spec, loop):
            return [(f"a {loop.kind} loop's push emit needs check, the command that must pass on the rebased tree "
                     "before the push lands")]
        approve = spec.get("approve", False)
        if isinstance(approve, dict) and (unknown := sorted(set(approve) - set(APPROVE_KEYS))):
            return [f"a push emit's approve table takes {', '.join(APPROVE_KEYS)}, not {', '.join(unknown)}"]
        if not isinstance(approve, bool | dict):
            return ["a push emit's approve is true, false, or a table of lines, deletes, paths and reviewer"]
        rules = approve_rules(spec)
        lines, globs = rules.get("lines", 0), rules.get("paths", [])
        if isinstance(lines, bool) or not isinstance(lines, int):
            return [f"a push emit's approve lines is a whole number of lines, such as 80, not {lines!r}"]
        if not isinstance(globs, str | list) or not all(isinstance(g, str) for g in globs):
            return [f"a push emit's approve paths is a list of path patterns, such as [\"src/auth/*\"], not {globs!r}"]
        if "reviewer" in rules and (not isinstance(reviewer := rules["reviewer"], str) or blank(reviewer)):
            return [(f"a push emit's approve reviewer is the model that reviews a held push, such as \"opus\", "
                     f"not {reviewer!r}")]
        return []

    def batches(self, findings: list[dict], outcome: dict) -> list[list[dict]]:
        return [findings] if outcome.get("branch") else []

    def render(self, loop: Loop, run_id: str, spec: dict, findings: list[dict], outcome: dict) -> dict:
        src = Path(outcome["workdir"])
        start, branch = outcome["start"], outcome["branch"]
        head = outcome.get("head") or util.git(["rev-parse", branch], src).strip()
        diff = diffguard.between(src, start, branch, moves=True)
        deleted = util.git_records(["diff", "--name-only", "--no-renames", "--diff-filter=D", start, branch], src)
        public = outcome.get("public", True)
        return {"mode": "land", "repo": loop.repo, "loop": loop.name, "branch": branch, "base": outcome["base"],
                "start": start, "head": head,
                "check": push_check(spec, loop),
                "timeout": loop.timeout_minutes * 60, "lines": diff.lines, "files": diff.files, "deleted": deleted,
                "title": outbound.plain(f"{loop.name}: run {run_id}" if public else outcome.get("title", ""), 120),
                "report": outbound.plain(outcome.get("facts", "") if public else outcome.get("report", ""), 6000),
                "about": outbound.plain(outcome.get("title", ""), 120), "story": outbound.plain(outcome.get("report", ""), 6000),
                "patch": shown_patch(vault.hidden(util.git(["diff", "--no-renames", start, branch], src)),
                                     f"git diff {start[:10]} {branch}"),
                "proof": outcome.get("proof")}

    def ruled(self, spec: dict, item: dict, cfg: config.Config) -> str:
        return self.only_a_person(spec, item, cfg) or self.over_limits(spec, item, cfg)

    def only_a_person(self, spec: dict, item: dict, cfg: config.Config) -> str:
        repo = push_repo(cfg, item)
        if repo is None:
            return f"repo {item.get('repo')!r} is not configured on this host"
        if reason := policy_refusal(repo, item["base"]):
            return reason
        layer = config.load_layer(repo.layer_dir)
        if layer.trust_level != "automerge":
            return f"the trust level of {item['repo']} is pr, so a person lands each push"
        if denied := [f for f in item["files"] if util.matches_any(f, diffguard.denied_paths(layer))]:
            return f"touches {', '.join(denied[:5])}, which only a person may change"
        if spec.get("approve") is True:
            return "approve is on"
        if touched := [f for f in item["files"] if util.matches_any(f, as_list(approve_rules(spec).get("paths")))]:
            return f"touches {', '.join(touched[:5])}, which a person reads"
        return ""

    def over_limits(self, spec: dict, item: dict, cfg: config.Config) -> str:
        rules = approve_rules(spec)
        if (limit := rules.get("lines")) and item["lines"] > int(limit):
            return f"{item['lines']} lines changed, over the {limit} line approval limit"
        if rules.get("deletes") and item["deleted"]:
            return f"deletes {', '.join(item['deleted'][:5])}"
        if jev.ready(cfg, [push_repo(cfg, item)]) and len(item.get("patch", "")) > PATCH_SHOWN:
            return f"the diff runs past the {PATCH_SHOWN} characters JEV reads"
        return ""

    def held(self, spec: dict, item: dict, cfg: config.Config, *, run_id: str = "") -> str:
        return self.ruled(spec, item, cfg) or ". ".join(self.objections(item, cfg, run_id=run_id).values())

    def reviewer(self, spec: dict, item: dict, cfg: config.Config) -> dict:
        if not (model := approve_rules(spec).get("reviewer")):
            return {}
        return {"model": model, "may_land": not self.only_a_person(spec, item, cfg)}

    def objections(self, item: dict, cfg: config.Config, *, run_id: str = "") -> dict[str, str]:
        if not jev.ready(cfg, [push_repo(cfg, item)]):
            return {}
        about = item.get("about") or item["title"]
        found = {}
        try:
            if held := jev.diff_screen(item.get("patch", ""), about, cfg, trace=(run_id, "push screen jev")):
                found["screen"] = f"JEV holds it: {'; '.join(held)}"
        except jev.Unjudged as e:
            found["unjudged screen"] = f"JEV could not judge the diff: {e}"
        if proof := item.get("proof"):
            found |= proof_objection(proof, item.get("patch", ""), about, cfg, run_id)
        return found

    def execute(self, item: dict, batch: list[dict], loop_name: str, run_id: str, cfg: config.Config) -> str:
        raise RuntimeError("push emits deliver through the outbox sender")

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

    def rejected(self, item: dict, cfg: config.Config | None = None) -> None:
        repo = push_repo(cfg or config.load(), item)
        if repo is not None and repo.path.is_dir() and item.get("branch"):
            util.delete_branch(repo.path, item["branch"])


def approve_rules(spec: dict) -> dict:
    approve = spec.get("approve", False)
    return approve if isinstance(approve, dict) else {}


def push_repo(cfg: config.Config, item: dict) -> config.Repo | None:
    return config_repo(cfg, item.get("repo", ""))


def policy_refusal(repo: config.Repo | None, base: str) -> str:
    if repo is None:
        return ""
    if repo.landing == "pr":
        return f"{repo.name} requires a PR; direct push is disabled"
    if repo.unattended_environment == "none":
        return f"{repo.name} does not allow unattended deployment; direct push is disabled"
    if repo.target_branch and base != repo.target_branch:
        return f"{repo.name} now targets {repo.target_branch}, not {base}"
    return ""


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
        return {"unjudged proof": f"JEV could not judge {name}: {e}"}
    return {"proof": f"JEV doubts that {name} proves the problem: {'; '.join(doubts)}"} if doubts else {}


def shown_patch(patch: str, whole: str, limit: int = PATCH_SHOWN) -> str:
    patch = outbound.redact(patch)
    if len(patch) <= limit:
        return patch
    return f"{patch[:limit]}\n\ncut at {limit} characters; {whole} shows the rest"


@contextmanager
def branch_worktree(src: Path, branch: str, name: str) -> Iterator[Path]:
    wt = paths.WORKTREES_DIR / name
    util.git(["worktree", "remove", "--force", str(wt)], src, check=False)
    util.git(["worktree", "add", "-q", "--detach", str(wt), branch], src)
    try:
        yield wt
    finally:
        util.git(["worktree", "remove", "--force", str(wt)], src, check=False)


class NoLongerRebases(RuntimeError):
    def __init__(self, base: str, clashes: list[str], error: RuntimeError):
        self.clashes = clashes
        where = f"newer commits there also change {', '.join(clashes[:5])}" if clashes \
            else util.first_line(str(error), 300)
        super().__init__(f"the change no longer rebases onto origin/{base}, because {where}")


def rebase_onto(wt: Path, base: str) -> None:
    try:
        util.git_as_owner(["rebase", "-q", f"origin/{base}"], wt)
    except RuntimeError as e:
        clashes = util.git_records(["diff", "--name-only", "--diff-filter=U"], wt, check=False)
        util.git(["rebase", "--abort"], wt, check=False)
        raise NoLongerRebases(base, clashes, e) from e


def fast_forward(src: Path, base: str, sha: str) -> str:
    on = util.git(["rev-parse", "--abbrev-ref", "HEAD"], src, check=False).strip()
    if on != base:
        return f"{paths.short_path(src)} is on {on}, not {base}, so it was left alone"
    try:
        util.git(["merge", "-q", "--ff-only", sha], src)
    except RuntimeError as e:
        return f"{paths.short_path(src)} did not fast forward: {e}"
    return f"{paths.short_path(src)} fast forwarded"
