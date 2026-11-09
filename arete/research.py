from __future__ import annotations

import difflib
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import config, gh, jev, paths, runner, state, tracker, triage, util, verdicts
from .heartbeat import mark_loop, network_up_or_skip
from .pipeline import fill_template, make_worktree, prompt, remove_worktree, slug

REPORTS_DIR = paths.STATE_DIR / "research"
TOOLS = [*runner.READ_TOOLS, "WebSearch", "WebFetch"]
TIMEOUT_SECONDS = 40 * 60
BUDGET_USD = 5.0
SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "report": {"type": "string"}, "name": {"type": "string"}},
    "required": ["summary", "report"],
}
RELATED_CHARS = 6000
DONE, FAILED, HELD, RUNNING, LATER, PARENT, ANSWERED, KEPT = (
    "done", "failed", "held", "running", "later", "parent", "answered", "kept")
NO_RUN = (HELD, PARENT, ANSWERED, KEPT)
CHANGED = ("Does what changed in the ticket ask something different, or add a fact, decision, limit, deadline or "
           "resource the report should use? Answer no when it only rewords, fixes a typo, adds a bare link, or adds a "
           "comment that asks nothing new, such as a status note, a sprint move, a meeting time, thanks, a mention or "
           "a request for an update.")
ANSWERS = ("Which of these existing reports already delivers what this ticket asks for, so a new report would only "
           "repeat it? Pick none when the ticket asks for a different deliverable, such as steps, a scope, a list of "
           "tickets, an estimate or a decision the report does not give, even when it covers the same topic, and pick "
           "none for a report that belongs to a different project.")
NONE_ANSWERS = "none of these: the ticket asks for something these reports do not deliver"
TITLE_LINE = re.compile(r"\A#[ \t]+\S.*\n+")


def report_file(ref: str) -> Path:
    return REPORTS_DIR / f"{tracker.safe_filename(ref)}.md"


@dataclass(frozen=True)
class Home:
    repo: config.Repo
    layer: config.RepoLayer

    @property
    def folder(self) -> str:
        return self.layer.research_dir

    @property
    def base(self) -> str:
        return self.layer.base_branch or gh.default_branch(self.repo.github)


def home_of(src: triage.TriageSource, r: dict) -> Home | None:
    named = [repo for repo in src.repos if repo.name in set(r["repos"])]
    for repo in named or (list(src.repos) if len(src.repos) == 1 else []):
        layer = config.load_layer(repo.layer_dir)
        if layer.research_dir and repo.github:
            return Home(repo, layer)
    return None


def filing(h: Home | None, path: str) -> dict[str, str]:
    if not h:
        return {"filing": "Arete keeps the report and shows it under the ticket's title.",
                "shape": "Write no title line. Start with the answer.", "file_name": "leave it empty."}
    where = f"`{path}`" if path else f"a new file in `{h.folder}/`"
    return {"filing": (f"Arete files the report in the `{h.repo.name}` repo as {where}, beside the reports already in "
                       f"{h.repo.path / h.folder}. Read two or three of them first, and write this one the way they "
                       "are written: the same title line, header fields and sections."),
            "shape": "Write the whole file, title line included, and lead with the answer as the reports beside it do.",
            "file_name": ("leave it empty; the file keeps its name." if path else
                          "a short kebab-case file name for it, without `.md`, like the names of the reports beside it.")}


def claim_path(h: Home, name: str, title: str) -> str:
    base = h.base
    util.git(["fetch", "-q", "origin", base], h.repo.path)
    taken = set(util.git(["ls-tree", "--name-only", f"origin/{base}", f"{h.folder}/"], h.repo.path).split())
    stem = slug(name or title, 60)
    return next(path for n in range(1, 100)
                if (path := f"{h.folder}/{stem if n == 1 else f'{stem}-{n}'}.md") not in taken)


def file_report(cfg: config.Config, h: Home, r: dict, report: dict) -> str:
    path = report["filed_path"]
    tk = tracker.for_repo(cfg, h.repo)
    t = tracker.Ticket(id=candidate(r).id, ref=r["ref"], title=r["title"], url=r["url"])
    key = f"research-{tracker.safe_filename(r['ref'])}"
    branch = f"arete/{key}"
    subject = tk.pr_title(fill_template(h.layer.subject, config.RepoLayer.subject, kind="docs(research)", id=t.id,
                                        title=r["title"]), t)
    body = (f"{tk.pr_link(t, whole=True)}\n\n**Summary:** {report['summary']}\n\n"
            f"Arete researched [{r['ref']}]({r['url']}) for ${report['cost']:.2f} and files the report as `{path}`. "
            "When the ticket changes, arete researches it again and pushes the new report here.")
    base = h.base
    wt = make_worktree(h.repo, base, branch, key)
    try:
        (wt / path).parent.mkdir(parents=True, exist_ok=True)
        (wt / path).write_text(report_file(r["ref"]).read_text())
        util.git(["add", "--", path], wt)
        if not util.git(["diff", "--cached", "--name-only"], wt).strip():
            return gh.open_pr_url(h.repo.github, branch) or f"https://github.com/{h.repo.github}/blob/{base}/{path}"
        util.git(["commit", "-q", "-m", f"{subject}\n\nArete-Research: {r['ref']}", "--", path], wt)
        util.git(["push", "-q", "--force", "-u", "origin", branch], wt)
        if url := gh.open_pr_url(h.repo.github, branch):
            gh.pr_edit_body(url, body)
            return url
        return gh.pr_create(h.repo.github, branch, base, subject, body, labels=tk.pr_labels, cwd=wt)
    finally:
        remove_worktree(h.repo, wt)


def file_in_home(cfg: config.Config, h: Home, r: dict) -> tuple[bool, str]:
    report = state.research_get(r["ref"])
    try:
        url = file_report(cfg, h, r, report)
    except (RuntimeError, OSError) as e:
        return False, (f"report written to {paths.short_path(report_file(r['ref']))}, but filing it in {h.repo.name} "
                       f"failed, and the research loop tries again: {util.first_line(str(e), 300)}")
    state.research_keep(r["ref"], filed_url=url)
    return True, f"report filed in {h.repo.name} as {report['filed_path']}: {url}"


def file_unfiled(cfg: config.Config) -> list[str]:
    rows = {r["ref"]: r for r in tickets()}
    failed = []
    for report in state.research_rows():
        r = rows.get(report["ref"])
        if not r or report["status"] != DONE or not report["filed_path"] or report["filed_url"]:
            continue
        if (src := source_of(cfg, r)) and (h := home_of(src, r)):
            ok, why = file_in_home(cfg, h, r)
            print(f"{util.iso()} {r['ref']}: {why}", flush=True)
            failed += [] if ok else [f"{r['ref']}: {why}"]
    return failed


def tickets() -> list[dict]:
    rows = [r for r in state.triage_rows() if triage.standing(r) == triage.RESEARCH_WORK]
    return sorted(rows, key=lambda r: triage.moment(r["updated"]), reverse=True)


def due() -> list[dict]:
    return [r for r in tickets() if triage.in_sprint(r) and triage.research_due(r, state.research_get(r["ref"]))]


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


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:20]


def family(r: dict, rows: list[dict]) -> list[tuple[str, dict]]:
    parent = r.get("parent") or ""
    kin = []
    for o in rows:
        if o["ref"] == r["ref"]:
            continue
        if parent and o["ref"] == parent:
            kin.append(("parent", o))
        elif parent and o.get("parent") == parent:
            kin.append(("sibling", o))
        elif o.get("parent") == r["ref"]:
            kin.append(("child", o))
    return kin


def delta(old: str, new: str) -> str:
    a, b = old.split("\n"), new.split("\n")
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("delete", "replace"):
            out += ["Removed:", *a[i1:i2]]
        if op in ("insert", "replace"):
            out += ["Added:", *b[j1:j2]]
    return "\n".join(out)


def change_matters(cfg: config.Config, r: dict, prior: dict, said: str) -> float | None:
    trace = ("", f"research {r['ref']} changed jev")
    text = (f"Ticket {r['ref']}\n\n## Text the report was written from\n\n{prior['ticket']}\n\n"
            f"## What changed in the ticket since\n\n{delta(prior['ticket'], said)}\n\n"
            f"## The report's summary\n\n{prior['summary']}")
    a = jev.ask(text, {"changed": jev.yes_no(CHANGED)}, cfg, trace=trace).answers.get("changed")
    if a is None:
        return None
    score = verdicts.scored(a.yes, cfg.jev.research_rerun, over=False)
    kept = verdicts.holds(score)
    verdicts.record(jev.traced_run(trace), "changed", r["ref"], r["title"], {"changed": score},
                    f"kept the report, the change does not need it (p {a.yes:.2f})" if kept else "",
                    "skipped" if kept else "went on")
    return a.yes


def still_stands(cfg: config.Config, r: dict, prior: dict | None, said: str, basis: str, judged: bool) -> str:
    if not prior or prior["status"] not in (DONE, PARENT, ANSWERED):
        return ""
    if basis == prior["basis"] or prior["status"] == DONE and said == prior["ticket"]:
        return "its text has not changed since the last check; only its status, sprint or labels moved"
    if prior["status"] != DONE or not prior["ticket"] or not judged:
        return ""
    p = change_matters(cfg, r, prior, said)
    if p is None or p >= cfg.jev.research_rerun:
        return ""
    return f"JEV reads the change in its text as one the report does not need (p {p:.2f}), so the report stands"


def parent_hold(r: dict, ticket: triage.TicketText, kin: list[tuple[str, dict]]) -> str:
    children = [o["ref"] for relation, o in kin if relation == "child"]
    if not children or not ticket.thin():
        return ""
    return (f"it has no description of its own and its children {', '.join(children)} are research tickets, so their "
            "reports cover it")


def answered_by(cfg: config.Config, r: dict, ticket: triage.TicketText, done: list[tuple[str, dict, dict]],
                titles: dict[str, str]) -> tuple[str, float]:
    def part_of(o: dict) -> str:
        p = o.get("parent") or ""
        return f" (part of {p}: {titles[p]})" if p in titles else f" (part of {p})" if p else ""

    options = {o["ref"]: f"{o['title']}{part_of(o)}. Its report: {report['summary']}" for _, o, report in done}
    options["none"] = NONE_ANSWERS
    trace = ("", f"research {r['ref']} answered jev")
    a = jev.ask(f"Ticket {r['ref']}: {r['title']}\n\n{ticket.text}", {"answered": jev.pick(ANSWERS, options)}, cfg,
                trace=trace).answers.get("answered")
    if a is None:
        return "", 0.0
    p = 1 - a.probabilities.get("none", 0.0)
    top = str(a.value) if a.value in options and a.value != "none" else ""
    score = verdicts.scored(p, cfg.jev.research_answered)
    link = top if top and verdicts.holds(score) else ""
    verdicts.record(jev.traced_run(trace), "answered", r["ref"], r["title"], {"answered": score},
                    f"answered by the report for {link} (p {p:.2f})" if link else "", "skipped" if link else "went on")
    return link, p


def related(src: triage.TriageSource, r: dict, done: list[tuple[str, dict, dict]]) -> str:
    parts = []
    if r.get("parent"):
        try:
            if brief := src.brief(r["parent"]):
                parts.append(f"### Its parent ticket, {brief}")
        except tracker.ERRORS as e:
            print(f"{util.iso()} {r['ref']}: could not read its parent {r['parent']}: {e}", flush=True)
    for relation, o, report in done:
        text = report["report"]
        if len(text) > RELATED_CHARS:
            text = text[:RELATED_CHARS] + f"\n\n[cut at {RELATED_CHARS} characters]"
        parts.append(f"### Report already written for its {relation} {o['ref']}: {o['title']}\n\n{text}")
    if not parts:
        return "none"
    return ("These come from the same family of tickets. Build on the reports: cite them by ticket key where they "
            "already answer part of this ticket, and spend this report on what they do not cover.\n\n"
            + "\n\n".join(parts))


def research(cfg: config.Config, r: dict, force: bool = False) -> tuple[str, str]:
    ref = r["ref"]
    base = {"source": r["source"], "title": r["title"], "url": r["url"], "updated": r["updated"],
            "run_id": os.environ.get("ARETE_RUN_ID", ""), "answered_by": ""}
    src = source_of(cfg, r)
    if not src:
        return LATER, f"{r['source']} is no longer a triage source"
    try:
        ticket = src.ticket(candidate(r))
    except tracker.ERRORS as e:
        return LATER, f"could not read {ref}: {e}"
    text, said = ticket.text, ticket.said(r["title"])
    basis = digest(said)
    prior = state.research_get(ref)
    rows = tickets()
    reports = {x["ref"]: x for x in state.research_rows()}
    kin = family(r, rows)
    done = [(relation, o, reports[o["ref"]]) for relation, o in kin
            if o["ref"] in reports and reports[o["ref"]]["status"] == DONE and reports[o["ref"]]["report"]]
    if not force:
        judged = jev.ready(cfg, list(src.repos))
        if why := still_stands(cfg, r, prior, said, basis, judged):
            state.research_keep(ref, title=r["title"], url=r["url"], updated=r["updated"], basis=basis)
            return KEPT, why
        fresh = not (prior and prior["status"] == DONE)
        if fresh and (why := parent_hold(r, ticket, kin)):
            state.research_set(ref, **base, status=PARENT, error=why, basis=basis)
            return PARENT, why
        if fresh and judged and done:
            link, p = answered_by(cfg, r, ticket, done, {o["ref"]: o["title"] for o in rows})
            if link:
                why = f"the report for {link} already answers it (JEV p {p:.2f})"
                state.research_set(ref, **(base | {"answered_by": link}), status=ANSWERED, error=why, basis=basis)
                return ANSWERED, why
        if not judged:
            why = "JEV is off, so nothing screens the ticket"
            state.research_set(ref, **base, status=HELD, error=why)
            return HELD, why
        screened = jev.injection(f"{r['title']}\n\n{text}", cfg, trace=("", f"research {ref} injection jev"))
        if screened is None:
            return LATER, f"JEV could not screen {ref}"
        if screened.yes >= cfg.jev.injection:
            why = f"JEV reads text in it as instructions to an agent (p {screened.yes:.2f})"
            state.research_set(ref, **base, status=HELD, error=why)
            return HELD, why
    state.research_set(ref, **base, status=RUNNING, error="")
    named = set(r["repos"])
    repos = sorted(src.repos, key=lambda repo: repo.name not in named)
    dirs = list(dict.fromkeys(repo.path for repo in repos if repo.path.is_dir()))
    h = home_of(src, r)
    filed_path = prior["filed_path"] if h and prior else ""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"{util.iso()} {ref}: researching", flush=True)
    res = runner.run(prompt("research", ref=ref, title=r["title"], ticket=text, related=related(src, r, done),
                            repos="\n".join(f"- `{repo.name}` at {repo.path}" for repo in repos) or "none",
                            **filing(h, filed_path)),
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
    if not h:
        report_file(ref).write_text(f"# {ref}: {r['title']}\n\n{r['url']}\n\nResearched {util.iso()} for "
                                    f"${res.cost_usd:.2f}.\n\n**Summary:** {summary}\n\n{report}\n")
        state.research_set(ref, status=DONE, summary=summary, report=report, error="", cost=res.cost_usd,
                           ticket=said, basis=basis, filed_path="", filed_url="")
        return DONE, f"report written to {paths.short_path(report_file(ref))} (${res.cost_usd:.2f})"
    titled = report if TITLE_LINE.match(report) else f"# {r['title']}\n\n{report}"
    try:
        filed_path = filed_path or claim_path(h, str(answer.get("name") or ""), r["title"])
    except RuntimeError as e:
        state.research_set(ref, status=FAILED, error=f"could not read {h.repo.name}: {e}", cost=res.cost_usd)
        return FAILED, f"could not read {h.repo.name}: {util.first_line(str(e), 300)}"
    report_file(ref).write_text(f"{titled.rstrip()}\n")
    state.research_set(ref, status=DONE, summary=summary, report=TITLE_LINE.sub("", titled, count=1), error="",
                       cost=res.cost_usd, ticket=said, basis=basis, filed_path=filed_path, filed_url="")
    ok, why = file_in_home(cfg, h, r)
    return DONE if ok else FAILED, f"{why} (${res.cost_usd:.2f})"


def next_due(dry_run: bool = False, asked: list[dict] | None = None) -> int:
    paths.ensure_dirs()
    cfg = config.load()
    with util.lock("research") as got:
        if not got:
            print(f"{util.iso()} research already running" + ("; ask again when it ends" if asked else ""))
            return 1 if asked else 0
        if dry_run:
            todo = due()
            print(f"would research {todo[0]['ref'] if todo else 'nothing'}"
                  + (f"; {len(todo) - 1} more due after it" if len(todo) > 1 else ""))
            return 0
        tidy()
        if not network_up_or_skip("research", dry_run):
            return 1 if asked else 0
        unfiled = file_unfiled(cfg)
        if asked:
            return research_asked(cfg, asked)
        todo = due()
        if not todo:
            mark_loop("research", not unfiled, unfiled[0] if unfiled else "nothing due")
            return 1 if unfiled else 0
        for r in todo:
            outcome, why = research(cfg, r)
            print(f"{util.iso()} {r['ref']}: {outcome}: {why}", flush=True)
            if outcome not in NO_RUN:
                break
        mark_loop("research", outcome in (DONE, *NO_RUN), why)
        return 1 if outcome in (FAILED, LATER) else 0


def research_asked(cfg: config.Config, asked: list[dict]) -> int:
    rows = {r["ref"].lower(): r for r in tickets()}
    outcomes = []
    for event in asked:
        ref = str(event.get("ref") or "")
        if not (r := rows.get(ref.lower())):
            outcome, why = FAILED, "it is no longer a research ticket on the triage board"
        else:
            ref = r["ref"]
            outcome, why = research(cfg, r, force=event.get("force") is True)
        print(f"{util.iso()} {ref}: {outcome}: {why}", flush=True)
        outcomes.append(outcome)
    mark_loop("research", FAILED not in outcomes and LATER not in outcomes, why)
    return 1 if FAILED in outcomes or LATER in outcomes else 0


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
    return 0 if outcome in (DONE, KEPT) else 1


def listing() -> str:
    rows = tickets()
    if not rows:
        return "no research tickets on the triage board"
    width = max(len(r["ref"]) for r in rows)
    lines = []
    for r in rows:
        report = state.research_get(r["ref"])
        notes = [triage.research_note(r, report), report and report["status"] == DONE and report["filed_url"]]
        lines += [f"{r['ref']:<{width}}  {r['title']}", *(f"{'':<{width}}  {n}" for n in notes if n)]
    return "\n".join(lines + ["", "`arete research REF` prints a report; `--run` runs one now."])


def shown(ref: str) -> str | None:
    report = state.research_get(ref)
    if report and report["status"] == ANSWERED and (linked := shown(report["answered_by"])):
        return f"{report['ref']}: not researched: {report['error']}\n\n{linked}"
    if report and report["report"]:
        text = report_file(report["ref"]).read_text() if report_file(report["ref"]).exists() else report["report"]
        if not report["filed_path"]:
            return text
        where = report["filed_url"] or "not filed yet; the research loop tries again"
        return f"{report['ref']}: {report['filed_path']}: {where}\n\n{text}"
    r = next((r for r in tickets() if r["ref"].lower() == ref.strip().lower()), None)
    if not r and not report:
        return None
    return f"{ref}: {triage.research_note(r, report) if r else report['status']}"
