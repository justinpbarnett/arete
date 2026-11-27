from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import config, factory, gh, paths, run_lines, util, workspace
from .review import Review, repair_fields

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
    repair_state: dict = field(default_factory=dict)


def report_review(report: dict) -> Review:
    saved = report.get("review")
    if not isinstance(saved, dict):
        return Review()
    problems = [{"file": p.get("file"), "problem": p["text"], "severity": "blocker", "category": p.get("category"),
                 **repair_fields(p)} for p in saved.get("problems") or []
                if isinstance(p, dict) and str(p.get("text") or "").strip()]
    return Review(verdict=saved.get("verdict"), trees=saved.get("trees") or {}, problems=problems).identify(
        f"{report.get('run')}:report-{run_lines.seq_of(report)}")


def saved_review(run_id: str) -> tuple[Review, str, dict]:
    from . import flows

    related = flows.lineage_runs(run_id)
    report = next((line for line in reversed(related.get(run_id, [])) if line.get("event") == run_lines.END), {})
    restored = flows.continued_review(related, report=report)
    if not (review := restored.get("review")):
        raise Refused(f"run {run_id} saved no review")
    if review.approved():
        raise Refused(f"run {run_id}: its last review approved the change, so nothing is left to repair")
    if review.verdict != "request_changes" or not review.blocking():
        raise Refused(f"run {run_id} requested changes but saved no findings to repair")
    state = restored["repair_state"]
    keys = {p["finding"] for p in review.blocking()} | {
        key for key, finding in state.get("findings", {}).items() if finding.get("verification") == "check"}
    state = {name: {key: value for key, value in rows.items() if key in keys}
             for name, rows in state.items() if name in ("findings", "receipts", "baselines", "in_use_waivers")}
    line = restored["line"]
    return review, f"the {line['event']} line of run {line['run']} (seq {run_lines.seq_of(line)})", state


def saved_plan(run_id: str) -> tuple[str, str, str]:
    from . import flows

    if adaptive := flows.adaptive_state(run_id):
        job, ticket = adaptive.get("job", {}), adaptive.get("prepared", {}).get("ticket", {})
        task = str(job.get("plan_text") or "\n".join(str(ticket.get(key) or "") for key in ("title", "body")))
        if task.strip():
            return task, str(job.get("goal") or ""), str(job.get("left_for_a_person") or "")
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
    review, evidence, state = saved_review(run_id)
    plan, goal, left = saved_plan(run_id)
    runs = {run_id} | factory.chain_runs(run_id)
    return Source(run_id, str(row["branch"]), str(row["pr_url"]).split(), review, evidence, plan, goal, left, runs, state)


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
