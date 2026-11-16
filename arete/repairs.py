from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import config, factory, gh, paths, run_lines, util, workspace
from .review import Review

PLAN_STEPS = ("plan", "revise", "repair")
MAX_LOG = 200
TRAILER = "%H%x09%(trailers:key=Arete-Run,valueonly,separator=%x2C)"


class Refused(ValueError):
    pass


@dataclass
class Source:
    run_id: str
    branch: str
    prs: list[str]
    review: Review
    evidence: str
    plan: str
    goal: str
    left_for_a_person: str
    runs: set[str] = field(default_factory=set)


def saved_review(run_id: str) -> tuple[Review, int]:
    report = next((line for line in reversed(run_lines.read(run_id)) if line.get("event") == run_lines.END), {})
    saved = report.get("review")
    if not isinstance(saved, dict) or not saved.get("verdict"):
        raise Refused(f"run {run_id} saved no review")
    if saved.get("verdict") == "approve":
        raise Refused(f"run {run_id}: its last review approved the change, so nothing is left to repair")
    listed = [p for p in saved.get("problems") or [] if isinstance(p, dict) and str(p.get("text") or "").strip()]
    if saved.get("verdict") != "request_changes" or not listed:
        raise Refused(f"run {run_id} requested changes but saved no findings to repair")
    found = Review.stored({"verdict": "request_changes", "problems":
        [{"file": p.get("file"), "problem": p["text"], "severity": "blocker", "category": p.get("category")}
         for p in listed]})
    return found, int(report.get("seq") or 0)


def saved_plan(run_id: str) -> tuple[str, str, str]:
    for data in reversed(factory.passed_data(factory.history(factory.chain(run_id)), *PLAN_STEPS)):
        if data.get("plan"):
            return str(data["plan"]), str(data.get("goal") or ""), str(data.get("left_for_a_person") or "")
    folder = paths.RUNS_DIR / run_id
    try:
        text = (folder / "plan.md").read_text()
    except OSError:
        text = ""
    structured = util.read_json(folder / "plan.json", {}).get("structured")
    structured = structured if isinstance(structured, dict) else {}
    if not text.strip():
        raise Refused(f"run {run_id} saved no plan to continue from")
    return text, str(structured.get("verify") or ""), str(structured.get("left_for_a_person") or "")


def source(row: dict) -> Source:
    run_id = str(row.get("id") or "")
    if row.get("only_step"):
        raise Refused(f"run {run_id} ran {row['only_step']} alone; repair the run it continued")
    if row.get("status") not in ("pr", "failed") or not row.get("pr_url") or not row.get("branch"):
        raise Refused(f"run {run_id} ended {row.get('status')} with no open PR; only a run that left a PR is repaired")
    review, seq = saved_review(run_id)
    plan, goal, left = saved_plan(run_id)
    runs = {run_id} | factory.chain_runs(run_id)
    return Source(run_id, str(row["branch"]), str(row["pr_url"]).split(), review,
                  f"the review in the report line of run {run_id} (seq {seq})", plan, goal, left, runs)


def run_commits(cwd: Path, log_args: list[str]) -> list[tuple[str, set[str]]]:
    out = util.git(["log", "--first-parent", f"--format={TRAILER}", *log_args], cwd)
    found = []
    for row in out.splitlines():
        sha, _, trailers = row.partition("\t")
        found.append((sha, {v.strip() for v in trailers.split(",")}))
    return found


def matched_head(cwd: Path, rev: str, found: Source) -> str:
    try:
        commits = run_commits(cwd, [f"-n{MAX_LOG}", rev])
    except RuntimeError as e:
        raise Refused(str(e)) from None
    for sha, runs in commits:
        if sha and found.runs and runs & found.runs:
            return sha
    raise Refused(f"{rev} holds no commit of run {found.run_id}, so its review cannot be matched to the PR head; "
                  "the branch was rewritten")


def open_heads(repo: config.Repo, found: Source) -> dict[str, str]:
    heads: dict[str, str] = {}
    for url in found.prs:
        view = gh.pr_view(url)
        if view.get("state") != "OPEN":
            raise Refused(f"{url} is {'merged' if view.get('mergedAt') else 'closed'}, so run {found.run_id} is "
                          "not repaired")
        if view.get("headRefName") != found.branch:
            raise Refused(f"{url} is on branch {view.get('headRefName')}, not {found.branch}, so run "
                          f"{found.run_id} is not repaired")
        part = next((p for p in repo.parts if p.github == gh.pr_repo(url)), None)
        if part is None:
            raise Refused(f"{url} is not in a repo of {repo.name}")
        src = repo.path / part.dir
        try:
            workspace.fetch(src, found.branch)
        except RuntimeError as e:
            raise Refused(f"could not fetch {found.branch} from origin: {util.first_line(str(e), 300)}") from None
        heads[part.github] = matched_head(src, f"origin/{found.branch}", found)
    return heads
