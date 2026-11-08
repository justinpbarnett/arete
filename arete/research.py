from __future__ import annotations

import os
from pathlib import Path

from . import config, jev, paths, runner, state, tracker, triage, util
from .heartbeat import mark_loop, network_up_or_skip
from .pipeline import prompt

REPORTS_DIR = paths.STATE_DIR / "research"
TOOLS = [*runner.READ_TOOLS, "WebSearch", "WebFetch"]
TIMEOUT_SECONDS = 40 * 60
BUDGET_USD = 5.0
SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "report": {"type": "string"}},
    "required": ["summary", "report"],
}
DONE, FAILED, HELD, RUNNING, LATER = "done", "failed", "held", "running", "later"


def report_file(ref: str) -> Path:
    return REPORTS_DIR / f"{tracker.safe_filename(ref)}.md"


def tickets() -> list[dict]:
    rows = [r for r in state.triage_rows() if triage.standing(r) == triage.RESEARCH_WORK]
    return sorted(rows, key=lambda r: triage.moment(r["updated"]), reverse=True)


def due() -> list[dict]:
    return [r for r in tickets() if triage.research_due(r, state.research_get(r["ref"]))]


def tidy() -> None:
    for report in state.research_rows():
        if report["status"] == RUNNING:
            state.research_set(report["ref"], status=FAILED, error="the run stopped before it finished")
    state.research_prune([r["ref"] for r in state.triage_rows()])


def source_of(cfg: config.Config, r: dict) -> triage.TriageSource | None:
    return next((s for s in triage.sources(cfg) if s.name == r["source"]), None)


def candidate(r: dict) -> triage.Candidate:
    tid = r["ref"].rsplit("#", 1)[-1] if r["tracker"] == "github" else r["ref"]
    return triage.Candidate(r["ref"], tid, r["title"], r["url"], r["updated"])


def research(cfg: config.Config, r: dict, force: bool = False) -> tuple[str, str]:
    ref = r["ref"]
    base = {"source": r["source"], "title": r["title"], "url": r["url"], "updated": r["updated"],
            "run_id": os.environ.get("ARETE_RUN_ID", "")}
    src = source_of(cfg, r)
    if not src:
        return LATER, f"{r['source']} is no longer a triage source"
    try:
        text = src.ticket_text(candidate(r))
    except tracker.ERRORS as e:
        return LATER, f"could not read {ref}: {e}"
    if not force:
        if not jev.ready(cfg, list(src.repos)):
            why = f"JEV is off, so nothing screens the ticket; `arete research {ref} --run --force`"
            state.research_set(ref, **base, status=HELD, error=why)
            return HELD, why
        screened = jev.injection(f"{r['title']}\n\n{text}", cfg, trace=("", f"research {ref} injection jev"))
        if screened is None:
            return LATER, f"JEV could not screen {ref}"
        if screened.yes >= cfg.jev.injection:
            why = (f"JEV reads text in it as instructions to an agent (p {screened.yes:.2f}); check the ticket, then "
                   f"`arete research {ref} --run --force`")
            state.research_set(ref, **base, status=HELD, error=why)
            return HELD, why
    state.research_set(ref, **base, status=RUNNING, error="")
    named = set(r["repos"])
    repos = sorted(src.repos, key=lambda repo: repo.name not in named)
    dirs = list(dict.fromkeys(repo.path for repo in repos if repo.path.is_dir()))
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"{util.iso()} {ref}: researching", flush=True)
    res = runner.run(prompt("research", ref=ref, title=r["title"], ticket=text,
                            repos="\n".join(f"- `{repo.name}` at {repo.path}" for repo in repos) or "none"),
                     dirs[0] if dirs else REPORTS_DIR, model=cfg.model("research"), tools=TOOLS, allowed_tools=TOOLS,
                     mcp_config=triage.NO_MCP_SERVERS, json_schema=SCHEMA, max_budget_usd=BUDGET_USD,
                     timeout=TIMEOUT_SECONDS, persist=False, add_dirs=dirs[1:], trace=("", f"research {ref}"), cfg=cfg)
    answer = res.structured if res.ok and isinstance(res.structured, dict) else {}
    report = str(answer.get("report") or "").strip()
    if not report:
        why = util.first_line(res.error or "the agent gave no report")
        state.research_set(ref, status=FAILED, error=why, cost=res.cost_usd)
        return FAILED, why
    summary = " ".join(str(answer.get("summary") or "").split())
    report_file(ref).write_text(f"# {ref}: {r['title']}\n\n{r['url']}\n\nResearched {util.iso()} for "
                                f"${res.cost_usd:.2f}.\n\n**Summary:** {summary}\n\n{report}\n")
    state.research_set(ref, status=DONE, summary=summary, report=report, error="", cost=res.cost_usd)
    return DONE, f"report written to {paths.short_path(report_file(ref))} (${res.cost_usd:.2f})"


def next_due(dry_run: bool = False) -> int:
    paths.ensure_dirs()
    cfg = config.load()
    with util.lock("research") as got:
        if not got:
            print(f"{util.iso()} research already running")
            return 0
        if dry_run:
            todo = due()
            print(f"would research {todo[0]['ref'] if todo else 'nothing'}"
                  + (f"; {len(todo) - 1} more due after it" if len(todo) > 1 else ""))
            return 0
        tidy()
        todo = due()
        if not todo:
            mark_loop("research", True, "nothing due")
            return 0
        if not network_up_or_skip("research", dry_run):
            return 0
        outcome, why = research(cfg, todo[0])
        print(f"{util.iso()} {todo[0]['ref']}: {outcome}: {why}")
        mark_loop("research", outcome in (DONE, HELD), why)
        return 1 if outcome in (FAILED, LATER) else 0


def run_now(ref: str, force: bool = False) -> int:
    paths.ensure_dirs()
    cfg = config.load()
    r = next((r for r in tickets() if r["ref"].lower() == ref.strip().lower()), None)
    if not r:
        print(f"{ref} is not a research ticket on the triage board; `arete research` lists them", flush=True)
        return 1
    with util.lock("research") as got:
        if not got:
            print("a research run is going; try again when it ends")
            return 1
        tidy()
        outcome, why = research(cfg, r, force=force)
    print(f"{r['ref']}: {outcome}: {why}")
    return 0 if outcome == DONE else 1


def listing() -> str:
    rows = tickets()
    if not rows:
        return "no research tickets on the triage board"
    width = max(len(r["ref"]) for r in rows)
    lines = []
    for r in rows:
        report = state.research_get(r["ref"])
        lines += [f"{r['ref']:<{width}}  {r['title']}", f"{'':<{width}}  {triage.research_note(r, report)}"]
    return "\n".join(lines + ["", "`arete research REF` prints a report; `--run` runs one now."])


def shown(ref: str) -> str | None:
    report = state.research_get(ref)
    if report and report["report"]:
        return report_file(report["ref"]).read_text() if report_file(report["ref"]).exists() else report["report"]
    r = next((r for r in tickets() if r["ref"].lower() == ref.strip().lower()), None)
    if not r and not report:
        return None
    return f"{ref}: {triage.research_note(r, report) if r else report['status']}"
